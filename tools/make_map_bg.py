#!/usr/bin/env python3
"""Build a map_bg PNG from a TerrainDumper dump (Phase 4).

Default style is raw at ship 4096:
  hillshade → edge fade + feather (outside terrain / mask cuts)
  → desaturate → levels → color balance → black 100 m grid @ 1.5
  → stipple → vignette 0.2 → paper grain σ=36

Edge fade runs *before* the post chain so vignette does not crush the border
ramp and the world grid stays visible outside the terrain. A hand-painted
exclusion mask at masks/<Scene>/mask.png is required (see make_mask_template.py).

Use --style charcoal for the old paper/ink look. Use --no-ship-post to skip
the raw post-process chain.

Border policy (all regions): terrain DEM `valid` defines the map edge.
Optional inset via --inset-frac / --inset-m (default off). Enrichment max-merges
heights only inside the DEM footprint.

Usage:
  python tools/make_map_bg.py <dump_dir> [--baseline map_bg.png] [--out map_bg_LakeRegion_new.png]
      [--size 4096] [--height 0] [--contour-m 1] [--shade-floor 0.18] [--style raw|charcoal]

Example:
  python tools/make_map_bg.py ^
    "I:/SteamLibrary/steamapps/common/TheLongDark/Mods/TerrainDumper/LakeRegion" ^
    --size 4096 ^
    --baseline "F:/Github/DetailedMaps/Maps/map_bg_LakeRegion_new.png" ^
    --out "F:/Github/TerrainDumper/out/maps/map_bg_LakeRegion_new.png"
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageOps

# Allow `python tools/make_map_bg.py` from repo root or tools/
sys.path.insert(0, str(Path(__file__).resolve().parent))
from apply_levels import apply_levels_value  # noqa: E402
from mapalign import (  # noqa: E402
    MapExtent,
    _is_backdrop_tile,
    apply_affine,
    collect_samples,
    fit_affine,
    invert_affine,
    list_terrain_stems,
    load_enrichment,
    load_exclusion_mask,
    load_json,
    load_tile_heights,
    main_tile_stem,
    map_xy_grid,
    output_size_for_extent,
    resolve_map_extent,
    enrichment_overlay_ids,
    enrichment_rock_ids,
    enrichment_structure_ids,
    warp_dem_to_image,
    warp_label_to_image,
    warp_ortho_to_image,
)

# Ship post-process defaults (GIMP presets tuned on LakeRegion 4k)
SHIP_OUTSIDE_RGB = (58, 55, 52)
SHIP_MASK_FAR_RGB = (100, 96, 90)
SHIP_LEVELS = dict(in_low=0.0, gamma=0.52, in_high=117.0, out_low=0.0, out_high=225.0)
SHIP_COLOR_BALANCE = dict(cyan_red=5.0, magenta_green=0.0, yellow_blue=-4.0)
SHIP_GRID_M = 100.0
SHIP_GRID_WIDTH = 1.5
SHIP_GRAIN_SIGMA = 36.0
SHIP_GRAIN_SEED = 42
SHIP_STIPPLE_DENSITY = 0.012
SHIP_STIPPLE_STRENGTH = 55.0
SHIP_STIPPLE_SEED = 99
SHIP_VIGNETTE_STRENGTH = 0.2
# Enrichment protrusion threshold (meters over a terrain DEM). Capture cell size is separate (0.25 m).
ENRICH_PROTRUDE_M = 0.5
# LongRail-style river/ice mesh draws over rocks in ortho. Enrichment top-hit on the
# open channel is a deep collider (~Y -47); rocks sit well above that plane.
PUNCH_WATER_DEEP_MAX_Y = -40.0
# Dark river sheet on elevated land (bridges stay brighter; see protect_class_ids).
PUNCH_WATER_DARK_LUMA = 90.0
PUNCH_WATER_NEAR_M = 16.0
PUNCH_WATER_SHRINK_M = 0.0  # off: shrink caused bright river-edge outlines
# Near-black structure (untextured railcars in ortho) — inpaint from brighter structure.
PUNCH_DARK_STRUCTURE_LUMA = 55.0


def punch_dark_ortho_over_elevated_enrichment(
    ortho_rgb: np.ndarray,
    ortho_valid: np.ndarray,
    enrichment_h: np.ndarray,
    *,
    deep_max_y: float = PUNCH_WATER_DEEP_MAX_Y,
    dark_luma_max: float = PUNCH_WATER_DARK_LUMA,
    near_m: float = PUNCH_WATER_NEAR_M,
    shrink_m: float = PUNCH_WATER_SHRINK_M,
    meters_per_pixel: float | None = None,
    enrichment_class: np.ndarray | None = None,
    protect_class_ids: set[int] | None = None,
    rock_class_ids: set[int] | None = None,
    dark_structure_luma: float = PUNCH_DARK_STRUCTURE_LUMA,
) -> tuple[np.ndarray, np.ndarray, int]:
    """Fix ortho where water/ice/tracks paint over rocks, and repair black structures.

    1. Elevated + near deep + dark ortho -> inpaint (river sheet on land)
    2. Rock class + elevated + near deep -> inpaint (ice/tracks through tunnel rocks)
    3. Structure class + near-black ortho -> inpaint from brighter structure
    Protect_class_ids skip (1)/(2) so bridges keep their ortho.
    Returns (ortho_rgb, ortho_valid, punched_pixel_count).
    """
    if ortho_rgb.shape[:2] != enrichment_h.shape or ortho_valid.shape != enrichment_h.shape:
        raise ValueError("ortho and enrichment grids must match for water punch-through")

    from scipy import ndimage

    out_rgb = np.asarray(ortho_rgb, dtype=np.float64).copy()
    out_valid = np.asarray(ortho_valid, dtype=bool).copy()
    luma = (
        0.299 * out_rgb[..., 0]
        + 0.587 * out_rgb[..., 1]
        + 0.114 * out_rgb[..., 2]
    )
    elevated = np.isfinite(enrichment_h) & (enrichment_h >= float(deep_max_y))
    deep = np.isfinite(enrichment_h) & (enrichment_h < float(deep_max_y))
    mpp = float(meters_per_pixel) if meters_per_pixel and meters_per_pixel > 0 else 2.0

    def dilate(mask: np.ndarray, metres: float) -> np.ndarray:
        if not metres or metres <= 0 or not mask.any():
            return np.zeros_like(mask, dtype=bool) if metres <= 0 else mask
        radius_px = max(1, int(np.ceil(float(metres) / mpp)))
        return ndimage.binary_dilation(mask, iterations=radius_px)

    near_deep = dilate(deep, near_m) if near_m > 0 else deep
    near_elev = dilate(elevated, shrink_m) if shrink_m > 0 else np.zeros_like(deep, dtype=bool)
    dark = luma < float(dark_luma_max)

    has_class = (
        enrichment_class is not None and enrichment_class.shape == enrichment_h.shape
    )
    protected = np.zeros_like(deep, dtype=bool)
    is_rock = np.zeros_like(deep, dtype=bool)
    is_structure = np.zeros_like(deep, dtype=bool)
    if has_class:
        if protect_class_ids:
            protected = np.isin(enrichment_class, list(protect_class_ids))
            is_structure = protected.copy()
        if rock_class_ids:
            is_rock = np.isin(enrichment_class, list(rock_class_ids))

    punch_elev = out_valid & elevated & near_deep & dark & ~protected
    punch_rock = out_valid & elevated & near_deep & is_rock & ~protected
    punch_shrink = out_valid & deep & near_elev & ~protected
    punch_dark_struct = (
        out_valid & is_structure & (luma < float(dark_structure_luma))
        if has_class and is_structure.any()
        else np.zeros_like(deep, dtype=bool)
    )
    punch = punch_elev | punch_rock | punch_shrink | punch_dark_struct
    if not punch.any():
        return out_rgb, out_valid, 0

    n_total = 0
    # Railcars / black structures: prefer brighter structure donors.
    if punch_dark_struct.any():
        safe_struct = out_valid & is_structure & (luma >= 100.0)
        if safe_struct.any():
            _, (iy_s, ix_s) = ndimage.distance_transform_edt(~safe_struct, return_indices=True)
            ys, xs = np.where(punch_dark_struct)
            out_rgb[ys, xs] = out_rgb[iy_s[ys, xs], ix_s[ys, xs]]
            n_total += int(punch_dark_struct.sum())
            punch = punch & ~punch_dark_struct
        # If no bright structure donors, fall through to land donors below.

    if not punch.any():
        return out_rgb, out_valid, n_total

    # Donors: land/rock ortho away from the channel.
    safe = out_valid & elevated & ~near_deep & (luma >= 100.0)
    if is_rock.any():
        safe_rock = out_valid & is_rock & elevated & ~near_deep & (luma >= 100.0)
        if safe_rock.any():
            safe = safe | safe_rock
    if not safe.any():
        safe = out_valid & elevated & ~deep
    if not safe.any():
        safe = out_valid & ~punch
    if not safe.any():
        return out_rgb, out_valid, n_total

    _, (iy, ix) = ndimage.distance_transform_edt(~safe, return_indices=True)
    ys, xs = np.where(punch)
    out_rgb[ys, xs] = out_rgb[iy[ys, xs], ix[ys, xs]]
    return out_rgb, out_valid, n_total + int(punch.sum())


def fill_invalid_nearest(dem: np.ndarray, valid: np.ndarray) -> np.ndarray:
    """Fill invalid cells from the nearest valid DEM sample to avoid border cliffs."""
    if not valid.any() or valid.all():
        return np.where(valid, dem, 0.0)

    from scipy import ndimage

    _, (iy, ix) = ndimage.distance_transform_edt(~valid, return_indices=True)
    filled = dem[iy, ix].copy()
    filled[valid] = dem[valid]
    return filled


def hillshade(dem: np.ndarray, valid: np.ndarray, altitude_deg: float = 45.0, azimuth_deg: float = 315.0) -> np.ndarray:
    z = fill_invalid_nearest(dem, valid)
    dy, dx = np.gradient(z)
    slope = np.pi / 2 - np.arctan(np.hypot(dx, dy))
    aspect = np.arctan2(-dx, dy)
    alt = np.radians(altitude_deg)
    az = np.radians(azimuth_deg)
    shaded = np.sin(alt) * np.sin(slope) + np.cos(alt) * np.cos(slope) * np.cos(az - aspect)
    shaded = np.clip(shaded, 0, 1)
    if valid.any():
        # normalize within valid
        v = shaded[valid]
        shaded = (shaded - v.min()) / max(1e-9, v.max() - v.min())
    return np.where(valid, shaded, 0.0)


def contour_edges(dem: np.ndarray, valid: np.ndarray, interval_m: float) -> np.ndarray:
    """Binary edge mask where contours cross."""
    if not valid.any() or interval_m <= 0:
        return np.zeros(dem.shape, dtype=bool)
    vals = dem[valid]
    z0 = np.floor(vals.min() / interval_m) * interval_m
    z1 = vals.max() + interval_m
    levels = np.arange(z0, z1, interval_m)
    edges = np.zeros(dem.shape, dtype=bool)
    filled = np.where(valid, dem, vals.min() - interval_m * 10)
    for level in levels:
        above = filled >= level
        e = (above != np.roll(above, 1, axis=0)) | (above != np.roll(above, 1, axis=1))
        e &= valid & np.roll(valid, 1, axis=0) & np.roll(valid, 1, axis=1)
        edges |= e
    return edges


def contour_masks(
    dem: np.ndarray,
    valid: np.ndarray,
    interval_m: float,
) -> tuple[np.ndarray, np.ndarray]:
    """Return (gentle_mask, steep_mask) for one already-warped DEM tile."""
    if interval_m <= 0 or not valid.any():
        empty = np.zeros(dem.shape, dtype=bool)
        return empty, empty

    z = fill_invalid_nearest(dem, valid)
    dy, dx = np.gradient(z)
    slope = np.hypot(dx, dy)
    gentle = valid & (slope < 0.90)
    steep = valid & (slope >= 0.90)

    gentle_cont = contour_edges(dem, valid, interval_m) & gentle
    major_m = max(interval_m * 1.5, 1.5)
    steep_cont = contour_edges(dem, valid, major_m) & steep
    return gentle_cont, steep_cont


def stipple(h: int, w: int, density: float, rng: np.random.Generator) -> np.ndarray:
    """Sparse dark dots 0..1 (1 = ink)."""
    n = int(h * w * density)
    img = np.zeros((h, w), dtype=np.float64)
    ys = rng.integers(0, h, size=n)
    xs = rng.integers(0, w, size=n)
    img[ys, xs] = rng.uniform(0.3, 1.0, size=n)
    return img


def render_charcoal(
    dem: np.ndarray,
    valid: np.ndarray,
    *,
    contour_m: float = 15.0,
    paper: tuple[int, int, int] = (214, 210, 200),
    ink: tuple[int, int, int] = (28, 26, 24),
    seed: int = 42,
) -> Image.Image:
    h, w = dem.shape
    rng = np.random.default_rng(seed)

    shade = hillshade(dem, valid)
    # Lift shadow floor so valleys aren't crushed
    shade = 0.22 + shade * 0.78
    relief = 1.0 - shade
    relief = np.clip(relief * 1.0, 0, 1)

    # Lowlands darker wash
    if valid.any():
        z = dem.copy()
        z[~valid] = np.nan
        z_norm = (dem - np.nanmin(z)) / max(1e-9, np.nanmax(z) - np.nanmin(z))
        low = np.clip(1.0 - z_norm, 0, 1)
        low = np.where(valid, low, 0)
    else:
        low = np.zeros_like(dem)

    contours = contour_edges(dem, valid, contour_m)
    cont = contours.astype(np.float64)
    cont *= 0.65  # slightly darker than soft pass, still not hard black

    dots = stipple(h, w, density=0.012, rng=rng)
    dots = np.where(valid, dots * (0.35 + 0.65 * relief), 0)

    # Composition weights → ink amount 0..1
    ink_amt = np.zeros((h, w), dtype=np.float64)
    ink_amt += relief * 0.32  # softer shadows
    ink_amt += low * 0.12
    ink_amt += cont * 0.42  # contours: a touch darker than soft pass
    ink_amt += dots * 0.25
    ink_amt = np.clip(ink_amt, 0, 1)
    ink_amt = np.where(valid, ink_amt, 0)

    # Outside valid: dark textured border like charcoal frame
    outside = ~valid
    border_noise = rng.random((h, w)) * 0.15
    outside_tone = 0.12 + border_noise

    paper_a = np.array(paper, dtype=np.float64)
    ink_a = np.array(ink, dtype=np.float64)
    rgb = paper_a[None, None, :] * (1.0 - ink_amt[..., None]) + ink_a[None, None, :] * ink_amt[..., None]
    out_rgb = paper_a[None, None, :] * (1.0 - outside_tone[..., None]) + np.array([18, 16, 14])[
        None, None, :
    ] * outside_tone[..., None]
    rgb = np.where(valid[..., None], rgb, out_rgb)

    # Soft paper grain
    grain = rng.normal(0, 3.0, size=(h, w, 1))
    rgb = np.clip(rgb + grain, 0, 255)

    img = Image.fromarray(rgb.astype(np.uint8), mode="RGB")
    # Slight blur then sharpen for charcoal feel
    img = img.filter(ImageFilter.GaussianBlur(radius=0.6))
    # Mild contrast only — heavy autocontrast re-crushes shadows
    img = ImageOps.autocontrast(img, cutoff=0.5)
    return img.convert("RGBA")


def add_frame(img: Image.Image, margin: int = 48) -> Image.Image:
    """Rough dark charcoal border."""
    w, h = img.size
    canvas = Image.new("RGBA", (w, h), (22, 20, 18, 255))
    inner = img.crop((margin, margin, w - margin, h - margin))
    # feathered paste
    mask = Image.new("L", inner.size, 255)
    canvas.paste(inner, (margin, margin), mask)
    draw = ImageDraw.Draw(canvas)
    # jagged-ish outer stroke by multiple rects
    for i, alpha in enumerate((180, 120, 80)):
        inset = 4 + i * 3
        draw.rectangle(
            [inset, inset, w - 1 - inset, h - 1 - inset],
            outline=(10, 8, 8, alpha),
            width=3,
        )
    return canvas


def blend_with_ortho(
    charcoal: Image.Image,
    ortho_rgb: np.ndarray,
    ortho_valid: np.ndarray,
    dem_valid: np.ndarray,
    *,
    ortho_weight: float = 0.55,
) -> Image.Image:
    """Mix desaturated ortho texture under charcoal ink."""
    base = np.asarray(charcoal.convert("RGBA"), dtype=np.float64)
    h, w = dem_valid.shape
    if ortho_rgb.shape[0] != h or ortho_rgb.shape[1] != w:
        raise ValueError("ortho warp size mismatch")

    # luminance of ortho
    lum = (
        0.299 * ortho_rgb[..., 0]
        + 0.587 * ortho_rgb[..., 1]
        + 0.114 * ortho_rgb[..., 2]
    ) / 255.0
    # gentle charcoal recolor of ortho
    paper = np.array([214, 210, 200], dtype=np.float64)
    ink = np.array([40, 38, 36], dtype=np.float64)
    ortho_tone = paper[None, None, :] * (0.35 + 0.65 * lum[..., None]) + ink[None, None, :] * (
        1.0 - lum[..., None]
    ) * 0.35
    ortho_tone = np.clip(ortho_tone, 0, 255)

    use = dem_valid & ortho_valid
    out = base.copy()
    for c in range(3):
        ch = out[..., c]
        ch = np.where(
            use,
            (1.0 - ortho_weight) * ch + ortho_weight * ortho_tone[..., c],
            ch,
        )
        out[..., c] = ch
    return Image.fromarray(out.astype(np.uint8), "RGBA")


def enrichment_rim_mask(
    cover: np.ndarray,
    *,
    scale: float,
    dilate_scale: float = 0.5,
) -> np.ndarray:
    """Perimeter of a protrusion cover, optionally dilated for stroke weight.

    dilate_scale: rock default 0.5 (softer/thicker). Structure outlines use ~0
    for a 1 px sharp edge.
    """
    from scipy import ndimage

    if not cover.any():
        return np.zeros_like(cover, dtype=bool)
    eroded = ndimage.binary_erosion(cover, iterations=1)
    rim = cover & ~eroded
    dilate_iters = max(0, int(round(float(dilate_scale) * scale)))
    if dilate_iters <= 0:
        return rim
    return ndimage.binary_dilation(rim, iterations=dilate_iters)


def strip_rock_rim_at_structure(
    rock_rim: np.ndarray,
    structure: np.ndarray,
    *,
    scale: float,
) -> np.ndarray:
    """Drop rock outline where it meets structure — structure draws that edge."""
    if rock_rim is None or not rock_rim.any() or structure is None or not structure.any():
        return rock_rim
    from scipy import ndimage

    # Cover soft-blur bleed (rock sigma_scale 0.70) past the contact pixels.
    pad = max(2, int(np.ceil(0.70 * float(scale))) + 1)
    near_struct = ndimage.binary_dilation(structure, iterations=pad)
    cleared = int((rock_rim & near_struct).sum())
    out = rock_rim & ~near_struct
    if cleared:
        print(f"rock rim cleared at structure contact: {cleared} px (pad={pad})")
    return out


def _enrichment_soft_rim(
    rim: np.ndarray,
    *,
    scale: float,
    sigma_scale: float,
    strength: float,
) -> np.ndarray | None:
    """Gaussian-softened rim alpha in [0,1], or None if empty."""
    if rim is None or not rim.any():
        return None
    from scipy import ndimage

    sigma = max(0.0, float(sigma_scale) * scale)
    if sigma <= 1e-6:
        soft = rim.astype(np.float64)
    else:
        soft = ndimage.gaussian_filter(rim.astype(np.float64), sigma=sigma)
    if soft.max() > 0:
        soft /= soft.max()
    return np.clip(soft * float(strength), 0.0, 1.0)


def render_raw_composite(
    dem: np.ndarray,
    valid: np.ndarray,
    ortho_rgb: np.ndarray | None,
    ortho_valid: np.ndarray | None,
    *,
    display_valid: np.ndarray | None = None,
    contour_m: float = 1.0,
    contour_gentle_mask: np.ndarray | None = None,
    contour_steep_mask: np.ndarray | None = None,
    enrich_cover: np.ndarray | None = None,
    enrich_outline: np.ndarray | None = None,
    enrich_outline_structure: np.ndarray | None = None,
    enrich_rock_fill: np.ndarray | None = None,
    enrich_structure_fill: np.ndarray | None = None,
    shade_floor: float = 0.18,
    outside_rgb: tuple[int, int, int] = SHIP_OUTSIDE_RGB,
) -> Image.Image:
    """True-color ortho × hillshade + contour lines (no charcoal filter).

    enrich_cover: optional mask where enrichment protrusions (primary tile) suppress
    contour ink. enrich_outline: rock (or legacy combined) rim. enrich_outline_structure:
    thinner sharper rim for bridges/docks/buildings. When outline is omitted, a rim is
    derived from enrich_cover.
    enrich_rock_fill: enrichment rock class painted over ortho (rocks win over ice/tracks).
    enrich_structure_fill: keeps soft rock outline off structure contacts.
    shade_floor: lift applied to hillshade (lower = darker shadows). Default 0.18.
    """
    if display_valid is None:
        display_valid = valid

    shade = hillshade(dem, valid)
    floor = float(np.clip(shade_floor, 0.0, 0.95))
    shade_soft = floor + shade * (1.0 - floor)

    if ortho_rgb is not None and ortho_valid is not None:
        out = np.asarray(ortho_rgb, dtype=np.float64).copy()
        use = valid & ortho_valid
        for c in range(3):
            out[..., c] = np.where(use, out[..., c] * shade_soft, out[..., c])
        display_only = display_valid & ortho_valid & ~valid
        for c in range(3):
            out[..., c] = np.where(display_only, out[..., c], out[..., c])
        dem_only = valid & ~ortho_valid
        gray = shade_soft * 255.0
        for c in range(3):
            out[..., c] = np.where(dem_only, gray, out[..., c])
    else:
        gray = shade_soft * 255.0
        out = np.stack([gray, gray, gray], axis=-1)
        display_only = display_valid & ~valid
        for c in range(3):
            out[..., c] = np.where(display_only, 215.0, out[..., c])

    # Enrichment rocks on top of ortho — full class fill (top-hit rock wins visually).
    if enrich_rock_fill is not None and enrich_rock_fill.any():
        rock_on = enrich_rock_fill & display_valid
        if rock_on.any():
            rock_base = np.array([158.0, 150.0, 142.0], dtype=np.float64)
            for c in range(3):
                out[..., c] = np.where(rock_on, rock_base[c] * shade_soft, out[..., c])
            print(f"enrichment rock fill over ortho: {int(rock_on.sum())} px")

    enrich_soft = None
    if contour_m > 0:
        if contour_gentle_mask is None or contour_steep_mask is None:
            contour_gentle_mask, contour_steep_mask = contour_masks(dem, valid, contour_m)
        cont = contour_gentle_mask | contour_steep_mask
        if enrich_cover is not None:
            # Contour suppression stays primary-tile only so broad water/ice
            # deltas do not wipe contour ink across the map.
            cont &= ~enrich_cover
            contour_gentle_mask = contour_gentle_mask & ~enrich_cover
            contour_steep_mask = contour_steep_mask & ~enrich_cover

        scale = max(1.0, float(dem.shape[0]) / 2048.0)
        rim: np.ndarray | None = None
        if enrich_outline is not None:
            # Already the OR of per-tile rims — do not re-erode a merged fill.
            rim = enrich_outline
        elif enrich_cover is not None and enrich_outline_structure is None:
            rim = enrichment_rim_mask(enrich_cover, scale=scale)
        # Rock / legacy combined rim: thicker soft stroke.
        rock_soft = _enrichment_soft_rim(rim, scale=scale, sigma_scale=0.70, strength=0.88)
        # Soft rock stroke must not bleed onto structure — structure owns that edge.
        if (
            rock_soft is not None
            and enrich_structure_fill is not None
            and enrich_structure_fill.any()
        ):
            from scipy import ndimage

            pad = max(2, int(np.ceil(0.70 * scale)) + 1)
            block = ndimage.binary_dilation(enrich_structure_fill, iterations=pad)
            rock_soft = np.where(block, 0.0, rock_soft)
        # Structure rim: thin sharp stroke (drawn on top).
        struct_soft = _enrichment_soft_rim(
            enrich_outline_structure,
            scale=scale,
            sigma_scale=0.20,
            strength=0.92,
        )
        if rock_soft is None:
            enrich_soft = struct_soft
        elif struct_soft is not None:
            enrich_soft = np.maximum(rock_soft, struct_soft)
        else:
            enrich_soft = rock_soft

        ink = np.array([20.0, 18.0, 16.0])
        for c in range(3):
            out[..., c] = np.where(
                contour_gentle_mask,
                ink[c] * 0.75 + out[..., c] * 0.25,
                out[..., c],
            )
            out[..., c] = np.where(
                contour_steep_mask,
                ink[c] * 0.70 + out[..., c] * 0.30,
                out[..., c],
            )

    out[~display_valid] = outside_rgb
    # Apply enrichment rim after outside fill so cross-tile outlines outside the
    # primary DEM (ice-sheet border objects, etc.) stay visible.
    if enrich_soft is not None:
        ink = np.array([20.0, 18.0, 16.0])
        a = enrich_soft[..., None]
        out = out * (1.0 - a) + ink[None, None, :] * a
    return Image.fromarray(np.clip(out, 0, 255).astype(np.uint8), "RGB").convert("RGBA")


def _gimp_color_balance_map(
    value: np.ndarray,
    lightness: np.ndarray,
    shadows: float,
    midtones: float,
    highlights: float,
) -> np.ndarray:
    a, b, scale = 0.25, 0.333, 0.7
    sh = shadows * np.clip((lightness - b) / -a + 0.5, 0, 1) * scale
    mi = (
        midtones
        * np.clip((lightness - b) / a + 0.5, 0, 1)
        * np.clip((lightness + b - 1.0) / -a + 0.5, 0, 1)
        * scale
    )
    hi = highlights * np.clip((lightness + b - 1.0) / a + 0.5, 0, 1) * scale
    return value + sh + mi + hi


def apply_color_balance(
    img: Image.Image,
    *,
    cyan_red: float = 5.0,
    magenta_green: float = 0.0,
    yellow_blue: float = -4.0,
    preserve_luminosity: bool = True,
) -> Image.Image:
    """GIMP Color Balance; same CR/MG/YB applied to shadows, midtones, highlights."""
    rgba = img.mode == "RGBA"
    arr = np.asarray(img.convert("RGBA" if rgba else "RGB"), dtype=np.float64)
    rgb = arr[..., :3] / 255.0
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    lum = 0.299 * r + 0.587 * g + 0.114 * b
    cr, mg, yb = cyan_red / 100.0, magenta_green / 100.0, yellow_blue / 100.0
    r2 = np.clip(_gimp_color_balance_map(r, lum, cr, cr, cr), 0, 1)
    g2 = np.clip(_gimp_color_balance_map(g, lum, mg, mg, mg), 0, 1)
    b2 = np.clip(_gimp_color_balance_map(b, lum, yb, yb, yb), 0, 1)
    if preserve_luminosity:
        lum2 = 0.299 * r2 + 0.587 * g2 + 0.114 * b2
        scale = np.ones_like(lum)
        nz = lum2 > 1e-8
        scale[nz] = lum[nz] / lum2[nz]
        r2 = np.clip(r2 * scale, 0, 1)
        g2 = np.clip(g2 * scale, 0, 1)
        b2 = np.clip(b2 * scale, 0, 1)
    out_rgb = np.stack([r2, g2, b2], axis=-1) * 255.0
    if rgba:
        out = np.concatenate([out_rgb, arr[..., 3:4]], axis=2)
        return Image.fromarray(np.clip(out, 0, 255).astype(np.uint8), "RGBA")
    return Image.fromarray(np.clip(out_rgb, 0, 255).astype(np.uint8), "RGB")


def world_meter_grid_coverage(
    valid: np.ndarray,
    A_world_to_map: np.ndarray,
    map_extent: MapExtent,
    spacing_m: float,
    *,
    width_px: float = 1.5,
) -> np.ndarray:
    """Antialiased coverage [0,1] of world-meter grid lines in map image space.

    Distance is measured in *pixels* via the local |∇world| so stroke weight stays
    even when lines fall between pixel centres (hard world-tol masks alias to 1–2 px).
    """
    if spacing_m <= 0 or not valid.any() or width_px <= 0:
        return np.zeros(valid.shape, dtype=np.float64)

    B = invert_affine(A_world_to_map)
    h, w = valid.shape
    mx, my = map_xy_grid(w, h, map_extent)
    world = apply_affine(B, np.column_stack([mx.ravel(), my.ravel()]))
    wx = world[:, 0].reshape(h, w)
    wz = world[:, 1].reshape(h, w)

    # Metres per pixel of each world axis (handles slight affine rotation/scale).
    gwx_r, gwx_c = np.gradient(wx)
    gwz_r, gwz_c = np.gradient(wz)
    gwx = np.maximum(np.hypot(gwx_r, gwx_c), 1e-9)
    gwz = np.maximum(np.hypot(gwz_r, gwz_c), 1e-9)

    dist_m_x = np.abs(wx - np.round(wx / spacing_m) * spacing_m)
    dist_m_z = np.abs(wz - np.round(wz / spacing_m) * spacing_m)
    dist_px = np.minimum(dist_m_x / gwx, dist_m_z / gwz)

    half = 0.5 * float(width_px)
    # Soft 1 px falloff so every line has the same visual weight.
    coverage = np.clip(half + 0.5 - dist_px, 0.0, 1.0)
    return np.where(valid, coverage, 0.0)


def world_meter_grid_mask(
    valid: np.ndarray,
    A_world_to_map: np.ndarray,
    map_extent: MapExtent,
    spacing_m: float,
    *,
    width_px: float = 1.5,
) -> np.ndarray:
    """Boolean grid mask (coverage > 0.5). Prefer world_meter_grid_coverage for drawing."""
    return (
        world_meter_grid_coverage(
            valid, A_world_to_map, map_extent, spacing_m, width_px=width_px
        )
        > 0.5
    )


def apply_paper_grain(
    img: Image.Image,
    *,
    sigma: float = SHIP_GRAIN_SIGMA,
    seed: int = SHIP_GRAIN_SEED,
    mask: np.ndarray | None = None,
) -> Image.Image:
    """Additive Gaussian paper grain (applied after grid so lines get grain too)."""
    if sigma <= 0:
        return img
    rgba = img.mode == "RGBA"
    arr = np.asarray(img.convert("RGBA" if rgba else "RGB"), dtype=np.float64)
    rng = np.random.default_rng(seed)
    grain = rng.normal(0.0, float(sigma), size=(arr.shape[0], arr.shape[1], 1))
    if mask is not None:
        m = mask.astype(bool)[..., None]
        arr[..., :3] = np.where(m, np.clip(arr[..., :3] + grain, 0, 255), arr[..., :3])
    else:
        arr[..., :3] = np.clip(arr[..., :3] + grain, 0, 255)
    if rgba:
        return Image.fromarray(arr.astype(np.uint8), "RGBA")
    return Image.fromarray(arr[..., :3].astype(np.uint8), "RGB")


def apply_stipple(
    img: Image.Image,
    *,
    density: float = SHIP_STIPPLE_DENSITY,
    strength: float = SHIP_STIPPLE_STRENGTH,
    seed: int = SHIP_STIPPLE_SEED,
    mask: np.ndarray | None = None,
) -> Image.Image:
    """Sparse dark flecks (ink/soot). Optional mask limits candidate pixels."""
    if density <= 0 or strength <= 0:
        return img
    rgba = img.mode == "RGBA"
    arr = np.asarray(img.convert("RGBA" if rgba else "RGB"), dtype=np.float64)
    h, w = arr.shape[:2]
    rng = np.random.default_rng(seed)
    if mask is not None:
        ys_all, xs_all = np.where(mask.astype(bool))
        if ys_all.size == 0:
            return img
        n = int(ys_all.size * float(density))
        if n <= 0:
            return img
        pick = rng.choice(ys_all.size, size=n, replace=False)
        ys = ys_all[pick]
        xs = xs_all[pick]
    else:
        n = int(h * w * float(density))
        if n <= 0:
            return img
        ys = rng.integers(0, h, size=n)
        xs = rng.integers(0, w, size=n)
    amt = rng.uniform(0.4, 1.0, size=len(ys)) * float(strength)
    arr[ys, xs, :3] -= amt[:, None]
    arr[..., :3] = np.clip(arr[..., :3], 0, 255)
    if rgba:
        return Image.fromarray(arr.astype(np.uint8), "RGBA")
    return Image.fromarray(arr[..., :3].astype(np.uint8), "RGB")


def apply_void_blotches(
    img: Image.Image,
    outside: np.ndarray,
    *,
    amp: float = 22.0,
    blur_px: float = 90.0,
    seed: int = 17,
) -> Image.Image:
    """Low-frequency luminance clouds in the void only."""
    if amp <= 0 or blur_px <= 0 or not outside.any():
        return img
    from scipy import ndimage

    rgba = img.mode == "RGBA"
    arr = np.asarray(img.convert("RGBA" if rgba else "RGB"), dtype=np.float64)
    rng = np.random.default_rng(seed)
    noise = rng.normal(0.0, 1.0, size=outside.shape)
    soft = ndimage.gaussian_filter(noise, sigma=float(blur_px))
    soft = soft / (float(np.std(soft)) + 1e-6)
    delta = (soft * float(amp))[..., None]
    m = outside.astype(bool)[..., None]
    arr[..., :3] = np.where(m, np.clip(arr[..., :3] + delta, 0, 255), arr[..., :3])
    if rgba:
        return Image.fromarray(arr.astype(np.uint8), "RGBA")
    return Image.fromarray(arr[..., :3].astype(np.uint8), "RGB")


def apply_void_coarse_grain(
    img: Image.Image,
    outside: np.ndarray,
    *,
    sigma: float = 28.0,
    seed: int = 23,
) -> Image.Image:
    """Coarser paper grain restricted to the void (dual-scale when fine grain already ran)."""
    return apply_paper_grain(img, sigma=sigma, seed=seed, mask=outside)


def apply_void_heavy_stipple(
    img: Image.Image,
    outside: np.ndarray,
    *,
    density: float = 0.045,
    strength: float = 72.0,
    seed: int = 29,
) -> Image.Image:
    """Denser ink flecks in the void only."""
    return apply_stipple(
        img, density=density, strength=strength, seed=seed, mask=outside
    )


def apply_vignette(
    img: Image.Image,
    *,
    strength: float = SHIP_VIGNETTE_STRENGTH,
) -> Image.Image:
    """Soft radial darkening toward the edges."""
    if strength <= 0:
        return img
    rgba = img.mode == "RGBA"
    arr = np.asarray(img.convert("RGBA" if rgba else "RGB"), dtype=np.float64)
    h, w = arr.shape[:2]
    yy, xx = np.mgrid[0:h, 0:w]
    cy, cx = (h - 1) / 2.0, (w - 1) / 2.0
    r = np.sqrt(((yy - cy) / max(cy, 1e-6)) ** 2 + ((xx - cx) / max(cx, 1e-6)) ** 2)
    r = np.clip(r, 0, 1.35) / 1.35
    fall = (r**2) * float(strength) * 255.0
    arr[..., :3] = np.clip(arr[..., :3] - fall[..., None], 0, 255)
    if rgba:
        return Image.fromarray(arr.astype(np.uint8), "RGBA")
    return Image.fromarray(arr[..., :3].astype(np.uint8), "RGB")


def postprocess_ship_look(
    img: Image.Image,
    valid: np.ndarray,
    A_world_to_map: np.ndarray,
    map_extent: MapExtent,
    *,
    desaturate: bool = True,
    levels: bool = True,
    color_balance: bool = True,
    grid_m: float = SHIP_GRID_M,
    grid_width: float = SHIP_GRID_WIDTH,
    stipple: bool = True,
    vignette: bool = True,
    grain_sigma: float = SHIP_GRAIN_SIGMA,
    grain_seed: int = SHIP_GRAIN_SEED,
    stipple_density: float = SHIP_STIPPLE_DENSITY,
    stipple_strength: float = SHIP_STIPPLE_STRENGTH,
    vignette_strength: float = SHIP_VIGNETTE_STRENGTH,
    levels_params: dict | None = None,
    color_balance_params: dict | None = None,
) -> Image.Image:
    """Ship raw look: … → grid → stipple → vignette → paper grain."""
    out = img.convert("RGBA")
    if desaturate:
        rgb = ImageOps.grayscale(out.convert("RGB")).convert("RGB")
        a = out.getchannel("A")
        out = rgb.convert("RGBA")
        out.putalpha(a)
    if levels:
        lp = {**SHIP_LEVELS, **(levels_params or {})}
        out = apply_levels_value(out, **lp)
    if color_balance:
        cp = {**SHIP_COLOR_BALANCE, **(color_balance_params or {})}
        out = apply_color_balance(out, **cp)
    if grid_m > 0:
        cov = world_meter_grid_coverage(
            valid, A_world_to_map, map_extent, grid_m, width_px=grid_width
        )
        if cov.any():
            arr = np.asarray(out.convert("RGBA"), dtype=np.float64)
            c = cov[..., None]
            arr[..., :3] = arr[..., :3] * (1.0 - c)
            out = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8), "RGBA")
    if stipple:
        out = apply_stipple(out, density=stipple_density, strength=stipple_strength)
    if vignette:
        out = apply_vignette(out, strength=vignette_strength)
    if grain_sigma > 0:
        out = apply_paper_grain(out, sigma=grain_sigma, seed=grain_seed)
    return out


def resolve_ortho_paths(dump: Path, ortho_arg: str) -> tuple[Path, Path] | None:
    """Return (png, meta) or None. Empty arg prefers classic tiled, then tiled_land."""
    if not ortho_arg:
        for stem in ("ortho_color_tiled", "ortho_color_tiled_land"):
            png = dump / f"{stem}.png"
            meta = dump / f"{stem}_meta.json"
            if png.exists() and meta.exists():
                return png, meta
        return None
    if ortho_arg.endswith(".png"):
        png = Path(ortho_arg)
        if not png.is_absolute():
            png = dump / ortho_arg
        meta = png.with_name(png.stem + "_meta.json")
    else:
        png = dump / f"{ortho_arg}.png"
        meta = dump / f"{ortho_arg}_meta.json"
    if png.exists() and meta.exists():
        return png, meta
    return None


def inset_valid_mask_m(
    valid: np.ndarray,
    A_world_to_map: np.ndarray,
    map_extent: MapExtent,
    inset_m: float,
) -> np.ndarray:
    """Erode valid mask inward by inset_m world meters (DEM border inset)."""
    if inset_m <= 0 or not valid.any():
        return valid

    from scipy import ndimage

    dz, dx = world_pixel_size(A_world_to_map, map_extent, valid.shape)
    dist_m = ndimage.distance_transform_edt(valid, sampling=(dz, dx))
    return valid & (dist_m >= inset_m)


def world_pixel_size(
    A_world_to_map: np.ndarray,
    map_extent: MapExtent,
    shape: tuple[int, int],
) -> tuple[float, float]:
    """(meters per row, meters per column) for an image-space raster.

    Uses Euclidean world step length so 90°-rotated maps (Rural, Tracks) work;
    axis-aligned Δz/Δx alone is ~0 when map axes are swapped.
    """
    B = invert_affine(A_world_to_map)
    h, w = shape
    mx, my = map_xy_grid(w, h, map_extent)
    world = apply_affine(B, np.column_stack([mx.ravel(), my.ravel()]))
    wx = world[:, 0].reshape(h, w)
    wz = world[:, 1].reshape(h, w)
    dz = max(float(np.median(np.hypot(np.diff(wx, axis=0), np.diff(wz, axis=0)))), 1e-3)
    dx = max(float(np.median(np.hypot(np.diff(wx, axis=1), np.diff(wz, axis=1)))), 1e-3)
    return dz, dx


def shade_outside(
    img: Image.Image,
    valid: np.ndarray,
    A_world_to_map: np.ndarray,
    map_extent: MapExtent,
    *,
    fade_m: float,
    lift: float,
    feather_m: float = 0.0,
    feather_strength: float = 0.0,
    feather_mode: str = "legacy",
    feather_zone: np.ndarray | None = None,
    feather_offset_m: float = 0.0,
) -> Image.Image:
    """Ramp everything outside the drawn terrain — region border and hand-masked cuts alike —
    from the outside fill at the terrain edge to a lighter tone away from it.

    feather_mode:
      legacy — wash outside fill inward over valid terrain (dark grows past the stroke).
      inset  — one fade across the painted edge: starts feather_offset_m onto the map,
               ends feather_m into the cut.
    """
    outside = ~valid
    if not outside.any() and not (
        feather_mode == "inset" and feather_zone is not None and feather_zone.any()
    ):
        return img

    from scipy import ndimage

    def smoothstep(x: np.ndarray) -> np.ndarray:
        x = np.clip(x, 0.0, 1.0)
        return x * x * (3.0 - 2.0 * x)

    dz, dx = world_pixel_size(A_world_to_map, map_extent, valid.shape)
    edge = np.array(SHIP_OUTSIDE_RGB, dtype=np.float64)
    far = np.array(SHIP_MASK_FAR_RGB, dtype=np.float64)
    arr = np.asarray(img.convert("RGBA"), dtype=np.float64)
    strength = float(np.clip(feather_strength, 0, 1))
    inset_m = float(feather_m)
    offset_m = float(max(feather_offset_m, 0.0))

    if outside.any() and fade_m > 0 and lift > 0:
        dist_m = ndimage.distance_transform_edt(outside, sampling=(dz, dx))
        t = smoothstep(dist_m / float(fade_m)) * float(np.clip(lift, 0.0, 1.0))
        ramp = edge[None, None, :] + (far - edge)[None, None, :] * t[..., None]
        arr[..., :3] = np.where(outside[..., None], ramp, arr[..., :3])

    if strength > 0 and (inset_m > 0 or offset_m > 0):
        if feather_mode == "inset" and feather_zone is not None and feather_zone.any():
            # Signed distance to the painted edge: + into the cut, - onto the map.
            # One smoothstep from -offset_m (full map) to +inset_m (outside fill).
            dist_in = ndimage.distance_transform_edt(feather_zone, sampling=(dz, dx))
            dist_out = ndimage.distance_transform_edt(~feather_zone, sampling=(dz, dx))
            signed = dist_in - dist_out
            span = inset_m + offset_m
            if span > 0:
                # Soft band only — deep void keeps the paper fade above.
                a = smoothstep((signed + offset_m) / span) * strength
                a = np.where((signed >= -offset_m) & (signed <= inset_m), a, 0.0)
                a = a[..., None]
                arr[..., :3] = arr[..., :3] * (1.0 - a) + edge[None, None, :] * a
        elif feather_mode != "inset" and inset_m > 0:
            inward_m = ndimage.distance_transform_edt(valid, sampling=(dz, dx))
            a = (1.0 - smoothstep(inward_m / inset_m)) * strength
            a = np.where(valid, a, 0.0)[..., None]
            arr[..., :3] = arr[..., :3] * (1.0 - a) + edge[None, None, :] * a

    return Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8), "RGBA")


def grow_mask_m(
    mask: np.ndarray,
    A_world_to_map: np.ndarray,
    map_extent: MapExtent,
    grow_m: float,
) -> np.ndarray:
    """Grow (or shrink, if negative) a mask by grow_m world meters."""
    if grow_m == 0 or not mask.any():
        return mask

    from scipy import ndimage

    dz, dx = world_pixel_size(A_world_to_map, map_extent, mask.shape)
    if grow_m > 0:
        dist_m = ndimage.distance_transform_edt(~mask, sampling=(dz, dx))
        return mask | (dist_m <= grow_m)
    dist_m = ndimage.distance_transform_edt(mask, sampling=(dz, dx))
    return mask & (dist_m >= -grow_m)


def resolve_mask_paths(dump: Path, scene: str, explicit: Path | None) -> tuple[Path, Path]:
    """(mask png, sidecar json) from --mask or <repo>/masks/<Scene>/mask.png."""
    region_dir = Path(__file__).resolve().parent.parent / "masks" / scene
    png = explicit if explicit is not None else region_dir / "mask.png"
    if not png.exists():
        raise FileNotFoundError(
            f"Missing exclusion mask {png}. "
            f"Run: python tools/make_mask_template.py <dump>  then paint masks/{scene}/mask.png"
        )
    sidecar = png.with_name("mask.json")
    if not sidecar.exists():
        # Legacy flat layout / old names inside the region folder.
        for candidate in (
            png.with_name(f"{scene}_mask.json"),
            region_dir / "mask.json",
            region_dir / f"{scene}_mask.json",
            region_dir.parent / f"{scene}_mask.json",
        ):
            if candidate.exists():
                sidecar = candidate
                break
    if not sidecar.exists():
        raise FileNotFoundError(
            f"Found {png} but no mask.json — run make_mask_template.py first"
        )
    return png, sidecar


def make_side_by_side(ours: Image.Image, baseline: Image.Image, out: Path) -> None:
    b = baseline.convert("RGBA").resize(ours.size, Image.Resampling.LANCZOS)
    gap = 8
    w, h = ours.size
    sheet = Image.new("RGB", (w * 2 + gap, h), (30, 30, 30))
    sheet.paste(b.convert("RGB"), (0, 0))
    sheet.paste(ours.convert("RGB"), (w + gap, 0))
    sheet.save(out)


def main() -> int:
    ap = argparse.ArgumentParser(description="TerrainDumper Phase 4 — make map_bg from dump")
    ap.add_argument("dump_dir", type=Path, help="Mods/TerrainDumper/<SceneName> folder")
    ap.add_argument("--baseline", type=Path, default=None, help="Existing map_bg for comparison (size ignored if --size set)")
    ap.add_argument("--out", type=Path, default=None, help="Output PNG path")
    ap.add_argument(
        "--size",
        type=int,
        default=0,
        help="Output width in pixels. 0 = 4096 ship default (or baseline width only if --use-baseline-size)",
    )
    ap.add_argument(
        "--height",
        type=int,
        default=0,
        help="Output height in pixels. 0 = fog UV aspect (or baseline height with --use-baseline-size)",
    )
    ap.add_argument(
        "--use-baseline-size",
        action="store_true",
        help="When --size is 0 and --baseline is set, inherit baseline pixel size instead of 4096",
    )
    ap.add_argument("--contour-m", type=float, default=1.0, help="Contour interval in meters")
    ap.add_argument(
        "--shade-floor",
        type=float,
        default=0.18,
        help="Raw hillshade lift 0..0.95 (lower=darker shadows); default 0.18",
    )
    ap.add_argument(
        "--tile",
        type=str,
        default=None,
        help="Primary terrain tile stem (default: highest-resolution tile in the dump)",
    )
    ap.add_argument("--enrichment", action="store_true", help="Merge enrichment_heights.raw via max()")
    ap.add_argument("--no-enrichment", action="store_true", help="Do not merge enrichment even if present")
    ap.add_argument(
        "--no-punch-water-ortho",
        action="store_true",
        help="Keep dark ortho over rocks (skip enrichment-based river/ice punch-through)",
    )
    ap.add_argument(
        "--punch-deep-max-y",
        type=float,
        default=PUNCH_WATER_DEEP_MAX_Y,
        help=f"Enrichment Y below this = water/ice channel (default {PUNCH_WATER_DEEP_MAX_Y})",
    )
    ap.add_argument(
        "--punch-dark-luma",
        type=float,
        default=PUNCH_WATER_DARK_LUMA,
        help=f"Ortho luma below this on elevated enrichment is punched (default {PUNCH_WATER_DARK_LUMA})",
    )
    ap.add_argument(
        "--punch-near-m",
        type=float,
        default=PUNCH_WATER_NEAR_M,
        help=f"Punch elevated ortho within this many metres of deep channel (default {PUNCH_WATER_NEAR_M})",
    )
    ap.add_argument(
        "--punch-shrink-m",
        type=float,
        default=PUNCH_WATER_SHRINK_M,
        help=f"Also inpaint deep-channel ortho within this many metres of rock (default {PUNCH_WATER_SHRINK_M}; 0=off)",
    )
    ap.add_argument(
        "--ortho",
        type=str,
        default="",
        help="Ortho PNG stem/path (default: auto ortho_color_tiled if present)",
    )
    ap.add_argument("--ortho-weight", type=float, default=0.55, help="Charcoal-style ortho blend weight 0..1")
    ap.add_argument("--no-frame", action="store_true", help="Skip charcoal frame (charcoal style only)")
    ap.add_argument(
        "--inset-m",
        type=float,
        default=None,
        help="Erode DEM border inward by this many world meters (overrides --inset-frac)",
    )
    ap.add_argument(
        "--inset-frac",
        type=float,
        default=0.0,
        help="DEM border inset as fraction of min(terrain sizeX,sizeZ); default 0 (off). e.g. 0.0125 ≈25m on LakeRegion",
    )
    ap.add_argument(
        "--style",
        type=str,
        default="raw",
        choices=["raw", "charcoal"],
        help="raw = ortho×hillshade+contours + ship post; charcoal = paper/ink filter",
    )
    ap.add_argument(
        "--no-ship-post",
        action="store_true",
        help="Skip raw post-process (desaturate/levels/color/grid/stipple/vignette/grain)",
    )
    ap.add_argument("--grid-m", type=float, default=SHIP_GRID_M, help="World-meter grid spacing (0=off)")
    ap.add_argument(
        "--grid-width",
        type=float,
        default=SHIP_GRID_WIDTH,
        help="Grid stroke width in pixels (antialiased; default 1.5)",
    )
    ap.add_argument("--no-grid", action="store_true", help="Disable world grid")
    ap.add_argument(
        "--grain-sigma",
        type=float,
        default=SHIP_GRAIN_SIGMA,
        help="Paper grain Gaussian sigma after vignette (0=off); default 36",
    )
    ap.add_argument("--no-grain", action="store_true", help="Disable paper grain")
    ap.add_argument("--no-stipple", action="store_true", help="Disable ink stipple flecks")
    ap.add_argument(
        "--vignette",
        type=float,
        default=SHIP_VIGNETTE_STRENGTH,
        help="Edge vignette strength 0..1 (0=off); default 0.2",
    )
    ap.add_argument("--no-vignette", action="store_true", help="Disable vignette")
    ap.add_argument("--no-desaturate", action="store_true", help="Skip desaturate step")
    ap.add_argument("--no-levels", action="store_true", help="Skip levels step")
    ap.add_argument("--no-color-balance", action="store_true", help="Skip color balance step")
    ap.add_argument(
        "--map-radius",
        type=float,
        default=None,
        help="Override Panel_Map.MAP_RADIUS for image UV (±R). Default: fog_of_war.json mapRadiusConstant",
    )
    ap.add_argument(
        "--sample-extent",
        action="store_true",
        help="Legacy UV: frame to alignment-sample AABB+pad (misaligns in-game POIs)",
    )
    ap.add_argument(
        "--mask",
        type=Path,
        default=None,
        help="Hand-painted exclusion mask; default <repo>/masks/<Scene>/mask.png (required)",
    )
    ap.add_argument(
        "--no-mask",
        action="store_true",
        help="Skip the exclusion mask (debug only; ship builds require a painted mask)",
    )
    ap.add_argument(
        "--edge-fade-m",
        type=float,
        default=80.0,
        help="Everything outside the terrain ramps from the outside fill to lighter over N meters (0=flat); default 80",
    )
    ap.add_argument(
        "--edge-fade-lift",
        type=float,
        default=0.24,
        help="How far the far end of that ramp lifts toward paper tone, 0..1; default 0.24",
    )
    ap.add_argument(
        "--edge-feather-m",
        type=float,
        default=20.0,
        help="Inset feather width into the cut in meters (inset mode), or legacy wash width; default 20",
    )
    ap.add_argument(
        "--edge-feather-offset-m",
        type=float,
        default=20.0,
        help="Inset mode: start the same fade this many meters onto the map; default 20",
    )
    ap.add_argument(
        "--edge-feather-strength",
        type=float,
        default=0.9,
        help="Feather opacity scale 0..1; default 0.9",
    )
    ap.add_argument(
        "--edge-feather-mode",
        choices=("legacy", "inset"),
        default="inset",
        help="inset=one fade across painted edge (default); legacy=wash dark inward over terrain",
    )
    ap.add_argument(
        "--grid-scope",
        choices=("terrain", "cut", "all"),
        default="all",
        help="Where the world grid draws: terrain only, terrain plus masked cut, or the whole image",
    )
    ap.add_argument(
        "--mask-grow-m",
        type=float,
        default=0.0,
        help="Grow (or shrink, if negative) the painted area by world meters",
    )
    ap.add_argument(
        "--void-texture-tests",
        action="store_true",
        help="Also write four void-interest variants next to --out (blotch / coarse_grain / void_stipple / all)",
    )
    args = ap.parse_args()

    dump: Path = args.dump_dir
    if not (dump / "alignment_samples.json").exists():
        print(f"Missing alignment_samples.json in {dump} — open map once and re-dump.")
        return 1
    tile = args.tile or main_tile_stem(dump)
    if not (dump / f"{tile}_meta.json").exists():
        print(f"Missing {tile}_meta.json")
        return 1

    samples = load_json(dump / "alignment_samples.json")
    W, M = collect_samples(samples, prefer_tile=tile)
    if len(W) < 3:
        print("Need >=3 alignment samples")
        return 1
    A = fit_affine(W, M)
    err = np.linalg.norm(apply_affine(A, W) - M, axis=1)
    print(f"affine RMSE (map units): {err.mean():.4f}")

    extent = resolve_map_extent(
        dump,
        M,
        map_radius=args.map_radius,
        use_sample_extent=args.sample_extent,
    )
    print(
        f"map UV extent: [{extent[0]:.1f},{extent[1]:.1f}]..[{extent[2]:.1f},{extent[3]:.1f}] "
        f"span={extent[2]-extent[0]:.1f}"
        + (" (sample AABB)" if args.sample_extent else " (MAP_RADIUS/detailScale)")
    )

    meters, meta = load_tile_heights(dump, tile)
    print(
        f"tile {tile}: {meta['raw']['width']}x{meta['raw']['height']} "
        f"meters {np.nanmin(meters):.1f}..{np.nanmax(meters):.1f}"
    )

    width = args.size
    height = args.height
    if width <= 0:
        if args.use_baseline_size and args.baseline and args.baseline.exists():
            width, height = Image.open(args.baseline).size
            print(f"size from baseline: {width}x{height}")
        else:
            width = 4096
    if height <= 0:
        width, height = output_size_for_extent(width, extent)
        print(f"size {width}x{height} (fog UV aspect)")
    elif args.size > 0 or not (
        args.use_baseline_size and args.baseline and args.baseline.exists()
    ):
        print(f"size {width}x{height}")

    dem, valid, edge_dist = warp_dem_to_image(
        meters, meta, A, extent, width, height, return_edge_distance=True
    )
    display_valid = valid.copy()
    backdrop_cover = np.zeros_like(valid, dtype=bool)
    # Multi-tile regions (Ash Canyon 4x4, etc.): union every tile into the DEM.
    # Water/ice sheets widen the footprint; the painted exclusion mask cuts them.
    extra_tiles = 0
    for stem in list_terrain_stems(dump):
        if stem == tile:
            continue
        try:
            tmeters, tmeta = load_tile_heights(dump, stem)
            tdem, tvalid, tedge_dist = warp_dem_to_image(
                tmeters, tmeta, A, extent, width, height, return_edge_distance=True
            )
        except Exception as ex:
            print(f"skip DEM tile {stem}: {ex}")
            continue
        if _is_backdrop_tile(tmeta) or float(tmeta.get("heightNormalizedMax", 0.0)) == 0.0:
            backdrop_cover = backdrop_cover | (tvalid & np.isfinite(tdem))
            display_valid = display_valid | (tvalid & np.isfinite(tdem))
            extra_tiles += 1
            continue
        both = valid & tvalid & np.isfinite(dem) & np.isfinite(tdem)
        only_t = tvalid & np.isfinite(tdem) & ~valid
        prefer_t = both & (tedge_dist > edge_dist)
        dem = np.where(prefer_t, tdem, dem)
        dem = np.where(only_t, tdem, dem)
        valid = valid | (tvalid & np.isfinite(tdem))
        display_valid = display_valid | (tvalid & np.isfinite(tdem))
        edge_dist = np.where(prefer_t, tedge_dist, edge_dist)
        edge_dist = np.where(only_t, tedge_dist, edge_dist)
        extra_tiles += 1
    coverage = float(display_valid.mean())
    if extra_tiles:
        print(f"warped DEM coverage: {coverage*100:.1f}% of {width}x{height} (primary + {extra_tiles} tiles)")
    else:
        print(f"warped DEM coverage: {coverage*100:.1f}% of {width}x{height}")

    enrich_cover: np.ndarray | None = None
    enrich_outline: np.ndarray | None = None
    enrich_outline_structure: np.ndarray | None = None
    enrich_rock_fill: np.ndarray | None = None
    enrich_structure_fill: np.ndarray | None = None
    enrich_h_for_punch: np.ndarray | None = None
    enrich_class_for_punch: np.ndarray | None = None
    protect_ids_for_punch: set[int] | None = None
    rock_ids_for_punch: set[int] | None = None
    use_enrich = args.enrichment or (
        not args.no_enrichment and (dump / "enrichment_meta.json").exists()
    )
    if use_enrich:
        loaded = load_enrichment(dump)
        if loaded is None:
            print("Enrichment requested but enrichment_meta.json missing")
        else:
            emeters, _emask, emeta = loaded
            edem, evalid = warp_dem_to_image(emeters, emeta, A, extent, width, height)
            enrich_h_for_punch = np.where(evalid & np.isfinite(edem), edem, np.nan)
            # max merge inside terrain footprint only — DEM defines the border
            both = valid & evalid & np.isfinite(edem) & np.isfinite(dem)
            delta = np.where(both, edem - dem, 0.0)
            dem = np.where(both, np.maximum(dem, edem), dem)

            scale = max(1.0, float(max(width, height)) / 2048.0)
            classes = emeta.get("classes")
            if classes is not None:
                eclass, cvalid = warp_label_to_image(classes, emeta, A, extent, width, height)
                enrich_class_for_punch = np.where(cvalid, eclass.astype(np.int16), np.int16(-1))
                labels = emeta.get("classLabels")
                rock_ids = enrichment_rock_ids(labels)
                struct_ids = enrichment_structure_ids(labels)
                protect_ids_for_punch = struct_ids
                rock_ids_for_punch = rock_ids
                overlay_ids = rock_ids | struct_ids
                rock = cvalid & np.isin(eclass, list(rock_ids))
                structure = cvalid & np.isin(eclass, list(struct_ids))
                overlay = rock | structure
                protrude = both & (delta > ENRICH_PROTRUDE_M)
                enrich_cover = overlay & protrude
                # Full rock class fill over ortho (top-hit rocks win visually).
                enrich_rock_fill = rock.copy()
                enrich_structure_fill = structure.copy()
                # Rim rock/structure that protrude inside DEM, or sit outside primary footprint.
                outside = evalid & ~valid
                rock_rim_cover = rock & (protrude | outside)
                struct_rim_cover = structure & (protrude | outside)
                enrich_outline = enrichment_rim_mask(rock_rim_cover, scale=scale)
                enrich_outline = strip_rock_rim_at_structure(
                    enrich_outline, structure, scale=scale
                )
                enrich_outline_structure = enrichment_rim_mask(
                    struct_rim_cover, scale=scale, dilate_scale=0.0
                )
                hist = []
                flat = eclass[cvalid]
                for i, name in enumerate(labels or []):
                    n = int(np.count_nonzero(flat == i))
                    if n:
                        hist.append(f"{name}={n}")
                print(
                    f"merged enrichment inside DEM border: cells={int(both.sum())} "
                    f"class-overlay cover={int(enrich_cover.sum())} "
                    f"rock_fill={int(enrich_rock_fill.sum())} "
                    f"rock_rim={int(enrich_outline.sum())} "
                    f"structure_rim={int(enrich_outline_structure.sum())} "
                    f"[{', '.join(hist)}] "
                    f"Y={np.nanmin(emeters):.1f}..{np.nanmax(emeters):.1f}"
                )
            else:
                # Legacy: protrusions vs primary tile — contour suppression only
                enrich_cover = both & (delta > ENRICH_PROTRUDE_M)

                # Outline rim: rim each tile's protrusions separately, then OR.
                # ORing fills first then taking one perimeter merges objects away.
                enrich_outline = np.zeros_like(valid, dtype=bool)
                tile_bits: list[str] = []
                for stem in list_terrain_stems(dump):
                    try:
                        tmeters, tmeta = load_tile_heights(dump, stem)
                    except Exception as ex:
                        print(f"skip outline tile {stem}: {ex}")
                        continue
                    tdem, tvalid = warp_dem_to_image(tmeters, tmeta, A, extent, width, height)
                    overlap = tvalid & evalid & np.isfinite(tdem) & np.isfinite(edem)
                    d = np.where(overlap, edem - tdem, 0.0)
                    protrude = overlap & (d > ENRICH_PROTRUDE_M)
                    rim = enrichment_rim_mask(protrude, scale=scale)
                    enrich_outline |= rim
                    tile_bits.append(f"{stem}:cover={int(protrude.sum())}/rim={int(rim.sum())}")
                print(
                    f"merged enrichment inside DEM border: cells={int(both.sum())} "
                    f"protrude>0.5m primary={int(enrich_cover.sum())} "
                    f"outline_rim_union={int(enrich_outline.sum())} "
                    f"[{', '.join(tile_bits)}] "
                    f"Y={np.nanmin(emeters):.1f}..{np.nanmax(emeters):.1f}"
                )
    elif (dump / "enrichment_meta.json").exists():
        print("enrichment_meta.json present (auto-merge on; pass --no-enrichment to skip)")

    inset_m = 0.0
    if args.inset_m is not None:
        inset_m = float(args.inset_m)
    elif args.inset_frac and args.inset_frac > 0:
        side = min(float(meta["size"]["x"]), float(meta["size"]["z"]))
        inset_m = float(args.inset_frac) * side
        print(f"inset-frac {args.inset_frac} = {inset_m:.2f} m (terrain side {side:.1f} m)")
    if inset_m > 0:
        before = float(valid.mean())
        valid = inset_valid_mask_m(valid, A, extent, inset_m)
        if enrich_cover is not None:
            enrich_cover = enrich_cover & valid
        # Keep outline candidates outside the eroded primary DEM so ice-sheet
        # border objects from other tiles can still rim.
        print(f"DEM border inset {inset_m:.1f} m: coverage {before*100:.1f}% -> {valid.mean()*100:.1f}%")
    else:
        print("DEM border inset off")

    # Hand-painted out-of-bounds areas. Only subtracts from valid — extent, size and the
    # affine are untouched, so in-game POI placement is unaffected. Required for ship builds.
    excluded: np.ndarray | None = None
    scene = load_json(dump / "meta.json").get("sceneName") or dump.name
    if args.no_mask:
        print("exclusion mask: skipped (--no-mask)")
    else:
        try:
            mask_png, sidecar = resolve_mask_paths(dump, scene, args.mask)
        except FileNotFoundError as e:
            print(str(e))
            return 1
        painted, mask_meta = load_exclusion_mask(mask_png, sidecar)
        warped, wvalid = warp_dem_to_image(painted, mask_meta, A, extent, width, height)
        # Subpixel fog-UV / mask AABB mismatch can leave a 1px canvas strip outside
        # wvalid. If that strip stays ~excluded, inset feather treats it as a painted
        # edge and darkens the first inset_m as a solid bar. Fold the gap into excluded.
        outside_template = ~wvalid
        excluded = grow_mask_m(
            wvalid & (warped > 0.5), A, extent, float(args.mask_grow_m)
        ) | outside_template
        before = float(display_valid.mean())
        cut = excluded
        if args.edge_feather_mode == "inset" and excluded.any():
            inset_m = float(args.edge_feather_m)
            if inset_m > 0:
                # Keep map under the soft band inside the paint; hard-remove only the core.
                from scipy import ndimage

                dz, dx = world_pixel_size(A, extent, excluded.shape)
                dist_in = ndimage.distance_transform_edt(excluded, sampling=(dz, dx))
                cut = excluded & (dist_in >= inset_m)
        valid = valid & ~cut
        display_valid = display_valid & ~cut
        backdrop_cover = backdrop_cover & ~cut
        if enrich_cover is not None:
            enrich_cover = enrich_cover & valid
        if enrich_outline is not None:
            enrich_outline = enrich_outline & ~cut
        if enrich_outline_structure is not None:
            enrich_outline_structure = enrich_outline_structure & ~cut
        if enrich_rock_fill is not None:
            enrich_rock_fill = enrich_rock_fill & ~cut & display_valid
        if enrich_structure_fill is not None:
            enrich_structure_fill = enrich_structure_fill & ~cut & display_valid
        pad_pct = float(outside_template.mean()) * 100.0
        print(
            f"exclusion mask {mask_png} (grow={args.mask_grow_m:g} m"
            f", feather_mode={args.edge_feather_mode}"
            f", inset={args.edge_feather_m:g} m"
            f", offset={args.edge_feather_offset_m:g} m"
            f", template_pad={pad_pct:.2f}%): "
            f"coverage {before*100:.1f}% -> {display_valid.mean()*100:.1f}%"
        )

    if args.grid_scope == "all":
        grid_valid = np.ones_like(display_valid)
    elif args.grid_scope == "cut" and excluded is not None:
        grid_valid = display_valid | excluded
    else:
        grid_valid = display_valid
    print(f"grid scope: {args.grid_scope}")
    contour_gentle_mask: np.ndarray | None = None
    contour_steep_mask: np.ndarray | None = None
    if args.style == "raw" and args.contour_m > 0:
        contour_gentle_mask = np.zeros_like(valid, dtype=bool)
        contour_steep_mask = np.zeros_like(valid, dtype=bool)
        for stem in list_terrain_stems(dump):
            tmeta = load_json(dump / f"{stem}_meta.json")
            if _is_backdrop_tile(tmeta) or float(tmeta.get("heightNormalizedMax", 0.0)) == 0.0:
                continue
            try:
                tmeters, _ = load_tile_heights(dump, stem)
                tdem, tvalid = warp_dem_to_image(tmeters, tmeta, A, extent, width, height)
            except Exception as ex:
                print(f"skip contour tile {stem}: {ex}")
                continue
            tvalid = tvalid & valid
            gentle_mask, steep_mask = contour_masks(tdem, tvalid, float(args.contour_m))
            contour_gentle_mask |= gentle_mask
            contour_steep_mask |= steep_mask

    owarp: np.ndarray | None = None
    ovalid: np.ndarray | None = None
    ortho_paths = resolve_ortho_paths(dump, args.ortho)
    if ortho_paths is None:
        if args.ortho:
            print(f"Ortho missing for --ortho {args.ortho!r}")
        elif args.style == "raw":
            print("No ortho_color_tiled found — raw style will use hillshade only")
    else:
        ortho_path, meta_path = ortho_paths
        ometa = load_json(meta_path)
        Image.MAX_IMAGE_PIXELS = None
        orgb = np.asarray(Image.open(ortho_path).convert("RGB"), dtype=np.uint8)
        owarp, ovalid = warp_ortho_to_image(orgb, ometa, A, extent, width, height)
        print(f"loaded ortho {ortho_path.name}")
        if (
            not args.no_punch_water_ortho
            and enrich_h_for_punch is not None
            and ovalid is not None
            and owarp is not None
        ):
            owarp, ovalid, n_punch = punch_dark_ortho_over_elevated_enrichment(
                owarp,
                ovalid,
                enrich_h_for_punch,
                deep_max_y=float(args.punch_deep_max_y),
                dark_luma_max=float(args.punch_dark_luma),
                near_m=float(args.punch_near_m),
                shrink_m=float(args.punch_shrink_m),
                meters_per_pixel=(extent[2] - extent[0]) / max(1, width - 1),
                enrichment_class=enrich_class_for_punch,
                protect_class_ids=protect_ids_for_punch,
                rock_class_ids=rock_ids_for_punch,
            )
            print(
                f"inpainted water-over-rock ortho: {n_punch} px "
                f"(deep_max_y={args.punch_deep_max_y:g}, dark_luma={args.punch_dark_luma:g}, "
                f"near_m={args.punch_near_m:g}, shrink_m={args.punch_shrink_m:g})"
            )

    if args.style == "raw":
        img = render_raw_composite(
            dem,
            valid,
            owarp,
            ovalid,
            display_valid=display_valid,
            contour_m=args.contour_m,
            contour_gentle_mask=contour_gentle_mask,
            contour_steep_mask=contour_steep_mask,
            enrich_cover=enrich_cover,
            enrich_outline=enrich_outline,
            enrich_outline_structure=enrich_outline_structure,
            enrich_rock_fill=enrich_rock_fill,
            enrich_structure_fill=enrich_structure_fill,
            shade_floor=args.shade_floor,
            outside_rgb=SHIP_OUTSIDE_RGB,
        )
        print(f"style=raw contour_m={args.contour_m} shade_floor={args.shade_floor}")
        img = shade_outside(
            img,
            valid,
            A,
            extent,
            fade_m=args.edge_fade_m,
            lift=args.edge_fade_lift,
            feather_m=args.edge_feather_m,
            feather_strength=args.edge_feather_strength,
            feather_mode=args.edge_feather_mode,
            feather_zone=excluded if args.edge_feather_mode == "inset" else None,
            feather_offset_m=args.edge_feather_offset_m,
        )
        print(
            f"edge fade: {args.edge_fade_m:g} m lift={args.edge_fade_lift:g} "
            f"feather inset={args.edge_feather_m:g} m offset={args.edge_feather_offset_m:g} m "
            f"@{args.edge_feather_strength:g} mode={args.edge_feather_mode}"
        )
        void_texture_variants: list[tuple[str, Image.Image]] | None = None
        if not args.no_ship_post:
            grid_m = 0.0 if args.no_grid else float(args.grid_m)
            grain_sigma = 0.0 if args.no_grain else float(args.grain_sigma)
            vig = 0.0 if args.no_vignette else float(args.vignette)
            img = postprocess_ship_look(
                img,
                grid_valid,
                A,
                extent,
                desaturate=not args.no_desaturate,
                levels=not args.no_levels,
                color_balance=not args.no_color_balance,
                grid_m=grid_m,
                grid_width=float(args.grid_width),
                stipple=not args.no_stipple,
                vignette=vig > 0,
                vignette_strength=vig,
                grain_sigma=grain_sigma,
            )
            print(
                f"ship-post: desat={not args.no_desaturate} levels={not args.no_levels} "
                f"color_balance={not args.no_color_balance} grid_m={grid_m} "
                f"grid_width={args.grid_width} stipple={not args.no_stipple} "
                f"vignette={vig} grain_sigma={grain_sigma}"
            )
            if args.void_texture_tests:
                outside = ~valid
                base = img.copy()
                void_texture_variants = [
                    ("blotch", apply_void_blotches(base.copy(), outside)),
                    ("coarse_grain", apply_void_coarse_grain(base.copy(), outside)),
                    ("void_stipple", apply_void_heavy_stipple(base.copy(), outside)),
                    (
                        "all",
                        apply_void_heavy_stipple(
                            apply_void_coarse_grain(
                                apply_void_blotches(base.copy(), outside), outside
                            ),
                            outside,
                        ),
                    ),
                ]
        else:
            print("ship-post: off")
            if args.void_texture_tests:
                print("void-texture-tests skipped (--no-ship-post)")
    else:
        void_texture_variants = None
        img = render_charcoal(dem, valid, contour_m=args.contour_m)
        if owarp is not None and ovalid is not None:
            img = blend_with_ortho(img, owarp, ovalid, valid, ortho_weight=args.ortho_weight)
            print(f"blended ortho weight={args.ortho_weight}")
        if not args.no_frame:
            img = add_frame(img, margin=max(24, min(width, height) // 48))
        print(f"style=charcoal contour_m={args.contour_m}")

    scene = load_json(dump / "meta.json").get("sceneName", "Region")
    out = args.out
    if out is None:
        out = dump / f"map_bg_{scene}_new.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out)
    print(f"wrote {out}")

    if void_texture_variants:
        for name, variant in void_texture_variants:
            vpath = out.with_name(f"{out.stem}_void_{name}.png")
            variant.save(vpath)
            print(f"wrote {vpath}")

    if args.baseline and args.baseline.exists():
        cmp = out.with_name(out.stem + "_vs_baseline.png")
        make_side_by_side(img, Image.open(args.baseline), cmp)
        print(f"wrote comparison {cmp}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
