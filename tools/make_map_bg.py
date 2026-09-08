#!/usr/bin/env python3
"""Build a map_bg PNG from a TerrainDumper dump (Phase 4).

Default style is raw at ship 4096 (charcoal-oriented ship post):
  hillshade → edge fade + feather (outside terrain / mask cuts)
  → void black fade 300 m (clean of grain) → desaturate → levels → color balance
  → black 100 m grid @ 1.5 → stipple → vignette 0.2 → paper grain σ=36

Use --pipeline color for the vintage color ship look (partial chroma, soft levels,
cool dim-gray edge tint). Charcoal SHIP_* presets stay the default.

Edge fade runs *before* the post chain so vignette does not crush the border
ramp and the world grid stays visible outside the terrain. Deep void black is
re-applied after grain so noise does not persist at the frame. A hand-painted
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
    "%TERRAIN_DUMPER_ROOT%/LakeRegion" ^
    --size 4096 ^
    --out "out/maps/map_bg_LakeRegion_new.png"
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
    enrichment_transport_ids,
    warp_dem_to_image,
    warp_label_to_image,
    warp_ortho_to_image,
)

# Ship post-process defaults (GIMP presets tuned on LakeRegion 4k) — charcoal / legacy
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

# Color / vintage pipeline (--pipeline color). Edge from TRN_Snow_Ground_B_Noise mean ×0.25/×0.35.
COLOR_OUTSIDE_RGB = (48, 51, 51)
COLOR_MASK_FAR_RGB = (67, 71, 72)
COLOR_LEVELS = dict(in_low=0.0, gamma=0.72, in_high=150.0, out_low=0.0, out_high=235.0)
COLOR_COLOR_BALANCE = dict(cyan_red=5.0, magenta_green=0.0, yellow_blue=-4.0)
COLOR_CHROMA_KEEP = 0.70  # 1=full color, 0=grayscale; charcoal path uses full desat instead
# Enrichment protrusion threshold (meters over a terrain DEM). Capture cell size is separate (0.25 m).
ENRICH_PROTRUDE_M = 0.5
# MiningRegion Concentrator: roof often lacks colliders so enrichment top-hit is rock under
# the building; rock fill then paints over good ortho. Keep ortho inside this world AABB only.
# (World XZ from portal cluster + building extent; MiningRegion-specific exception.)
CONCENTRATOR_ORTHO_OVER_ROCK = {
    "MiningRegion": (-340.0, 95.0, -180.0, 220.0),  # x0, z0, x1, z1
}
# LongRail-style river/ice mesh draws over rocks in ortho. Enrichment top-hit on the
# open channel is a deep collider (~Y -47); rocks sit well above that plane.
PUNCH_WATER_DEEP_MAX_Y = -40.0
# Dark river sheet on elevated land (bridges stay brighter; see protect_class_ids).
PUNCH_WATER_DARK_LUMA = 90.0
PUNCH_WATER_NEAR_M = 16.0
PUNCH_WATER_SHRINK_M = 0.0  # off: shrink caused bright river-edge outlines
# Near-black structure (untextured railcars in ortho) — inpaint from brighter structure.
PUNCH_DARK_STRUCTURE_LUMA = 55.0

# TLD albedo PNGs for enrichment rock fill (extracted via tools/_extract_rock_textures.py).
DEFAULT_ROCK_TEXTURE_DIR = Path(__file__).resolve().parent.parent / "out" / "textures" / "tld_rock_samples"
# Prefer land-only ortho for Blackrock outdoor + prison (ice sheet omitted from land dump).
_ORTHO_LAND_FIRST_SCENES = frozenset({"BlackrockRegion", "BlackrockPrisonSurvivalZone"})
ROCK_KIND_BARE_TEX = {
    "other": "TRN_RockCliff_08_A_Tiled",
    "cliff08": "TRN_RockCliff_08_A_Tiled",
    "cliff09": "TRN_Rock09_A",
    "cliff": "TRN_RockCliff_08_A_Tiled",
    "rock07": "TRN_Rock07_Spg",
    "rock08": "TRN_Rock08_C",
    "rock09": "TRN_Rock09_A",
    "rock04": "TRN_Rock04",
    "rockmid": "TRN_Rock09_A_Flat",
    "caverock": "OBJ_CaveRock_V9_D",
    "icecaverock": "OBJ_CaveRock_V9_D",
    "minerock": "TRN_Rock08_D",
    "gearrock": "TRN_Rock04",
    "boulder": "TRN_Rock08_E",
}
ROCK_KIND_SNOW_TEX = {
    # Real snow albedos — _Win rock tiles do not read as snow at map scale.
    "other": "TRN_Snow_Ground_B_Noise",
    "cliff08": "TRN_Snow_Ground_B_Noise",
    "cliff09": "TRN_Snow_Ground_B_Noise",
    "cliff": "TRN_Snow_Ground_B_Noise",
    "rock07": "TRN_Snow_Ground_B_Noise",
    "rock08": "TRN_Snow_Ground_B_Noise",
    "rock09": "TRN_Snow_Ground_B_Noise",
    "rock04": "TRN_Snow_Ground_A_Noise",
    "rockmid": "TRN_Snow_Ground_B_Noise",
    "caverock": "TRN_Snow_Ground_A_Noise",
    "icecaverock": "TRN_Snow_Ice_A",
    "minerock": "TRN_Snow_Ground_B_Noise",
    "gearrock": "TRN_Snow_Ground_B_Noise",
    "boulder": "TRN_Snow_Ground_B_Noise",
}
ROCK_SNOW_FALLBACK = "TRN_Snow_Ground_A_Noise"
ROCK_FLAT_RGB = np.array([158.0, 150.0, 142.0], dtype=np.float64)


def load_rock_texture_bank(texture_dir: Path | None) -> dict[str, np.ndarray]:
    """Load RGB float textures from PNGs; keys are stem names without extension."""
    if texture_dir is None or not texture_dir.is_dir():
        return {}
    bank: dict[str, np.ndarray] = {}
    for p in sorted(texture_dir.glob("*.png")):
        img = Image.open(p).convert("RGB")
        bank[p.stem] = np.asarray(img, dtype=np.float64)
    return bank


def _tile_rgb(tex: np.ndarray, h: int, w: int) -> np.ndarray:
    th, tw = tex.shape[:2]
    if th <= 0 or tw <= 0:
        return np.zeros((h, w, 3), dtype=np.float64)
    reps_y = (h + th - 1) // th
    reps_x = (w + tw - 1) // tw
    return np.tile(tex, (reps_y, reps_x, 1))[:h, :w]


def paint_enrichment_rock_textures(
    out: np.ndarray,
    rock_on: np.ndarray,
    shade_soft: np.ndarray,
    *,
    rock_kinds: np.ndarray | None,
    rock_kind_labels: list[str] | None,
    rock_snow: np.ndarray | None,
    texture_bank: dict[str, np.ndarray],
) -> str:
    """Paint rock_on pixels from tiled TLD albedos (kind + snow). Returns status string."""
    if not rock_on.any():
        return "none"
    h, w = rock_on.shape
    if not texture_bank:
        for c in range(3):
            out[..., c] = np.where(rock_on, ROCK_FLAT_RGB[c] * shade_soft, out[..., c])
        return f"flat_rgb px={int(rock_on.sum())}"

    tiled_cache: dict[str, np.ndarray] = {}

    def tiled(name: str) -> np.ndarray | None:
        if name not in texture_bank:
            return None
        if name not in tiled_cache:
            tiled_cache[name] = _tile_rgb(texture_bank[name], h, w)
        return tiled_cache[name]

    labels = list(rock_kind_labels or [])
    painted = 0
    used: dict[str, int] = {}

    # Default layer for any rock cell, then overwrite by kind/snow.
    def apply_tex(mask: np.ndarray, name: str, fallback: str | None = None) -> int:
        nonlocal painted
        if not mask.any():
            return 0
        tex = tiled(name)
        if tex is None and fallback:
            tex = tiled(fallback)
            name = fallback or name
        if tex is None:
            for c in range(3):
                out[..., c] = np.where(mask, ROCK_FLAT_RGB[c] * shade_soft, out[..., c])
            used["flat"] = used.get("flat", 0) + int(mask.sum())
            painted += int(mask.sum())
            return int(mask.sum())
        for c in range(3):
            out[..., c] = np.where(mask, tex[..., c] * shade_soft, out[..., c])
        used[name] = used.get(name, 0) + int(mask.sum())
        painted += int(mask.sum())
        return int(mask.sum())

    snow_mask = (
        rock_on & (rock_snow != 0)
        if rock_snow is not None
        else np.zeros_like(rock_on, dtype=bool)
    )
    bare_mask = rock_on & ~snow_mask

    if rock_kinds is None or not labels:
        apply_tex(bare_mask, ROCK_KIND_BARE_TEX["cliff08"])
        apply_tex(snow_mask, ROCK_KIND_SNOW_TEX["cliff08"], fallback=ROCK_SNOW_FALLBACK)
    else:
        # Unlabeled / kind 0 on rock cells → default cliff08 family.
        kind0 = rock_on & (rock_kinds == 0)
        apply_tex(kind0 & bare_mask, ROCK_KIND_BARE_TEX["cliff08"])
        apply_tex(kind0 & snow_mask, ROCK_KIND_SNOW_TEX["cliff08"], fallback=ROCK_SNOW_FALLBACK)
        for i, label in enumerate(labels):
            if i == 0 or label == "none":
                continue
            m = rock_on & (rock_kinds == i)
            if not m.any():
                continue
            bare_name = ROCK_KIND_BARE_TEX.get(label, ROCK_KIND_BARE_TEX["other"])
            snow_name = ROCK_KIND_SNOW_TEX.get(label, ROCK_KIND_SNOW_TEX["other"])
            apply_tex(m & bare_mask, bare_name)
            apply_tex(m & snow_mask, snow_name, fallback=ROCK_SNOW_FALLBACK)

    bits = ", ".join(f"{k}={v}" for k, v in sorted(used.items(), key=lambda kv: -kv[1])[:8])
    return f"textured px={painted} [{bits}]"


def world_aabb_mask(
    A: np.ndarray,
    extent: MapExtent,
    out_w: int,
    out_h: int,
    x0: float,
    z0: float,
    x1: float,
    z1: float,
) -> np.ndarray:
    """Boolean image mask for an axis-aligned world XZ box."""
    xa, xb = (float(x0), float(x1)) if x0 <= x1 else (float(x1), float(x0))
    za, zb = (float(z0), float(z1)) if z0 <= z1 else (float(z1), float(z0))
    mx, my = map_xy_grid(out_w, out_h, extent)
    B = invert_affine(A)
    world = apply_affine(B, np.column_stack([mx.ravel(), my.ravel()]))
    wx = world[:, 0].reshape(out_h, out_w)
    wz = world[:, 1].reshape(out_h, out_w)
    return (wx >= xa) & (wx <= xb) & (wz >= za) & (wz <= zb)


def concentrator_ortho_repost_mask(
    *,
    scene: str,
    A: np.ndarray,
    extent: MapExtent,
    width: int,
    height: int,
    structure: np.ndarray | None,
    ortho_valid: np.ndarray | None = None,
) -> np.ndarray | None:
    """MiningRegion Concentrator footprint for a post-enrichment ortho paint-back.

    Prefers hole-filled structure outline inside the Concentrator AABB; falls back to
    the AABB if structure coverage is too sparse.
    """
    box = CONCENTRATOR_ORTHO_OVER_ROCK.get(scene)
    if box is None:
        return None
    x0, z0, x1, z1 = box
    aabb = world_aabb_mask(A, extent, width, height, x0, z0, x1, z1)
    mpp = (float(extent[2]) - float(extent[0])) / max(1, width - 1)
    footprint = aabb
    if structure is not None and np.any(structure & aabb):
        from scipy import ndimage

        close_i = max(1, int(round(3.0 / mpp)))
        dilate_i = max(1, int(round(2.0 / mpp)))
        closed = ndimage.binary_closing(structure & aabb, iterations=close_i)
        filled = ndimage.binary_fill_holes(closed)
        filled = ndimage.binary_dilation(filled, iterations=dilate_i) & aabb
        # Need a real filled roof plate — not just thin walls.
        if int(filled.sum()) >= 2000:
            footprint = filled
    if ortho_valid is not None:
        footprint = footprint & ortho_valid
    if not footprint.any():
        return None
    print(
        f"Concentrator ortho repost footprint ({scene}): {int(footprint.sum())} px "
        f"AABB X[{x0:g},{x1:g}] Z[{z0:g},{z1:g}]"
    )
    return footprint


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
    enrich_rock_kinds: np.ndarray | None = None,
    enrich_rock_kind_labels: list[str] | None = None,
    enrich_rock_snow: np.ndarray | None = None,
    rock_texture_bank: dict[str, np.ndarray] | None = None,
    enrich_structure_fill: np.ndarray | None = None,
    ortho_repost_mask: np.ndarray | None = None,
    dem_ortho_repost: np.ndarray | None = None,
    shade_floor: float = 0.18,
    outside_rgb: tuple[int, int, int] = SHIP_OUTSIDE_RGB,
) -> Image.Image:
    """True-color ortho × hillshade + contour lines (no charcoal filter).

    enrich_cover: optional mask where enrichment protrusions (primary tile) suppress
    contour ink (rock/structure when elevated; road/path/rail on class footprint).
    enrich_outline: rock (or legacy combined) rim. enrich_outline_structure:
    thinner sharper rim for bridges/docks/buildings. When outline is omitted, a rim is
    derived from enrich_cover.
    enrich_rock_fill: enrichment rock class painted over ortho (rocks win over ice/tracks).
    enrich_rock_kinds / enrich_rock_snow: optional formatVersion-4 detail for textured fill.
    enrich_structure_fill: structure (+ road/path/rail) footprint — soft rock outline stays off these.
    ortho_repost_mask: after rock fill, paint ortho again here (Concentrator footprint).
    dem_ortho_repost: DEM used for hillshade on that repost (pre-enrichment / no under-roof rock).
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

    # Enrichment rocks on top of ortho — textured by kind/snow when bank present.
    if enrich_rock_fill is not None and enrich_rock_fill.any():
        rock_on = enrich_rock_fill & display_valid
        if rock_on.any():
            status = paint_enrichment_rock_textures(
                out,
                rock_on,
                shade_soft,
                rock_kinds=enrich_rock_kinds,
                rock_kind_labels=enrich_rock_kind_labels,
                rock_snow=enrich_rock_snow,
                texture_bank=rock_texture_bank or {},
            )
            print(f"enrichment rock fill over ortho: {status}")

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
        if ortho_repost_mask is not None and ortho_repost_mask.any():
            # Concentrator ortho repost owns this footprint — don't leave contour ink under it.
            contour_gentle_mask = contour_gentle_mask & ~ortho_repost_mask
            contour_steep_mask = contour_steep_mask & ~ortho_repost_mask

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

    # Hyper-specific: paint Concentrator ortho last so it covers rock fill, contours, and rims.
    if (
        ortho_repost_mask is not None
        and ortho_rgb is not None
        and ortho_valid is not None
        and ortho_repost_mask.any()
    ):
        m = ortho_repost_mask & ortho_valid & display_valid
        if m.any():
            if dem_ortho_repost is not None:
                shade_r = hillshade(dem_ortho_repost, valid)
                shade_r_soft = floor + shade_r * (1.0 - floor)
            else:
                shade_r_soft = shade_soft
            ortho_f = np.asarray(ortho_rgb, dtype=np.float64)
            for c in range(3):
                out[..., c] = np.where(m, ortho_f[..., c] * shade_r_soft, out[..., c])
            print(f"ortho repost over enrichment+contours: {int(m.sum())} px")

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
    chroma_keep: float | None = None,
) -> Image.Image:
    """Ship raw look: … → grid → stipple → vignette → paper grain.

    desaturate + chroma_keep=None → full grayscale (charcoal).
    desaturate + chroma_keep in [0,1] → keep that fraction of chroma (color vintage).
    """
    out = img.convert("RGBA")
    if desaturate:
        if chroma_keep is None:
            rgb = ImageOps.grayscale(out.convert("RGB")).convert("RGB")
        else:
            arr = np.asarray(out.convert("RGB"), dtype=np.float64)
            gray = (0.299 * arr[..., 0] + 0.587 * arr[..., 1] + 0.114 * arr[..., 2])[
                ..., None
            ]
            keep = float(np.clip(chroma_keep, 0.0, 1.0))
            mixed = gray + keep * (arr - gray)
            rgb = Image.fromarray(np.clip(mixed, 0, 255).astype(np.uint8), "RGB")
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


def resolve_ortho_paths(
    dump: Path,
    ortho_arg: str,
    *,
    scene: str | None = None,
) -> tuple[Path, Path] | None:
    """Return (png, meta) or None.

    Empty arg: most scenes prefer classic tiled, then tiled_land.
    BlackrockRegion / BlackrockPrisonSurvivalZone prefer tiled_land first.
    """
    if not ortho_arg:
        prefer_land = scene in _ORTHO_LAND_FIRST_SCENES
        stems = (
            ("ortho_color_tiled_land", "ortho_color_tiled")
            if prefer_land
            else ("ortho_color_tiled", "ortho_color_tiled_land")
        )
        for stem in stems:
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


def _smoothstep01(x: np.ndarray) -> np.ndarray:
    x = np.clip(x, 0.0, 1.0)
    return x * x * (3.0 - 2.0 * x)


# Soften medial-axis "circle seams" in the void without washing tint onto the map.
DEFAULT_EDGE_FADE_DIST_BLUR_M = 36.0


def outside_distance_m(
    outside: np.ndarray,
    *,
    dz: float,
    dx: float,
    blur_m: float = 0.0,
) -> np.ndarray:
    """Metres into the void (0 on the keep/cut boundary).

    When blur_m > 0, Gaussian-blur a *signed* distance field so medial-axis ridges
    soften while the zero contour (map edge) stays put. Plain EDT blur is avoided —
    it leaks interior zeros across the boundary and halos the map.
    """
    from scipy import ndimage

    if not outside.any():
        return np.zeros(outside.shape, dtype=np.float64)
    dist_out = ndimage.distance_transform_edt(outside, sampling=(dz, dx))
    blur = float(max(blur_m, 0.0))
    if blur <= 1e-6:
        return np.where(outside, dist_out, 0.0)
    dist_in = ndimage.distance_transform_edt(~outside, sampling=(dz, dx))
    signed = dist_out - dist_in
    sigma_px = (blur / max(dz, 1e-6), blur / max(dx, 1e-6))
    signed = ndimage.gaussian_filter(signed, sigma=sigma_px)
    return np.where(outside, np.maximum(signed, 0.0), 0.0)


def void_black_weight(
    valid: np.ndarray,
    A_world_to_map: np.ndarray,
    map_extent: MapExtent,
    *,
    fade_m: float,
    black_m: float,
    dist_blur_m: float = DEFAULT_EDGE_FADE_DIST_BLUR_M,
) -> np.ndarray:
    """0..1 blend toward pure black in the void (0 on/near terrain, 1 in deep void)."""
    outside = ~valid
    black_span = float(max(black_m, 0.0))
    if not outside.any() or fade_m <= 0 or black_span <= 0:
        return np.zeros(valid.shape, dtype=np.float64)

    dz, dx = world_pixel_size(A_world_to_map, map_extent, valid.shape)
    dist_m = outside_distance_m(outside, dz=dz, dx=dx, blur_m=dist_blur_m)
    t = _smoothstep01((dist_m - float(fade_m)) / black_span)
    return np.where(outside, t, 0.0)


def apply_clean_void_black(
    img: Image.Image,
    weight: np.ndarray,
    *,
    black_rgb: tuple[int, int, int] = (0, 0, 0),
) -> Image.Image:
    """Lerp toward black by weight — strips grain/stipple/grid in the deep void."""
    if weight.shape != img.size[::-1] or float(np.max(weight)) <= 0:
        return img
    rgba = img.mode == "RGBA"
    arr = np.asarray(img.convert("RGBA" if rgba else "RGB"), dtype=np.float64)
    black = np.array(black_rgb, dtype=np.float64)
    a = weight[..., None]
    arr[..., :3] = arr[..., :3] * (1.0 - a) + black[None, None, :] * a
    if rgba:
        return Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8), "RGBA")
    return Image.fromarray(np.clip(arr[..., :3], 0, 255).astype(np.uint8), "RGB")


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
    black_m: float = 0.0,
    black_rgb: tuple[int, int, int] = (0, 0, 0),
    dist_blur_m: float = DEFAULT_EDGE_FADE_DIST_BLUR_M,
    outside_rgb: tuple[int, int, int] = SHIP_OUTSIDE_RGB,
    mask_far_rgb: tuple[int, int, int] = SHIP_MASK_FAR_RGB,
) -> Image.Image:
    """Ramp everything outside the drawn terrain — region border and hand-masked cuts alike.

    Two-stop (black_m=0): outside fill at the terrain edge → lighter paper away from it.
    Three-stop (black_m>0): outside fill → paper lift over fade_m → black over black_m.
    Playable DEM (valid) is never recolored by the distance ramp.

    dist_blur_m: Gaussian blur (metres) on signed outside distance — softens
    medial-axis seams in the void without smearing across the map edge. 0 = sharp EDT.
    Inset feather stays sharp (blurring it washes outside tint onto the map).

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

    dz, dx = world_pixel_size(A_world_to_map, map_extent, valid.shape)
    edge = np.array(outside_rgb, dtype=np.float64)
    far = np.array(mask_far_rgb, dtype=np.float64)
    black = np.array(black_rgb, dtype=np.float64)
    arr = np.asarray(img.convert("RGBA"), dtype=np.float64)
    strength = float(np.clip(feather_strength, 0, 1))
    inset_m = float(feather_m)
    offset_m = float(max(feather_offset_m, 0.0))
    black_span = float(max(black_m, 0.0))

    if outside.any() and fade_m > 0 and lift > 0:
        dist_m = outside_distance_m(outside, dz=dz, dx=dx, blur_m=dist_blur_m)
        lift_c = float(np.clip(lift, 0.0, 1.0))
        paper = edge + (far - edge) * lift_c
        t_paper = _smoothstep01(dist_m / float(fade_m))
        ramp = edge[None, None, :] + (paper - edge)[None, None, :] * t_paper[..., None]
        if black_span > 0:
            t_black = _smoothstep01((dist_m - float(fade_m)) / black_span)
            ramp = ramp + (black - paper)[None, None, :] * t_black[..., None]
        arr[..., :3] = np.where(outside[..., None], ramp, arr[..., :3])

    if strength > 0 and (inset_m > 0 or offset_m > 0):
        if feather_mode == "inset" and feather_zone is not None and feather_zone.any():
            # Signed distance to the painted edge: + into the cut, - onto the map.
            # One smoothstep from -offset_m (full map) to +inset_m (outside fill).
            # Keep sharp — blurring this washes outside tint onto the map.
            dist_in = ndimage.distance_transform_edt(feather_zone, sampling=(dz, dx))
            dist_out = ndimage.distance_transform_edt(~feather_zone, sampling=(dz, dx))
            signed = dist_in - dist_out
            span = inset_m + offset_m
            if span > 0:
                # Soft band only — deep void keeps the paper/black fade above.
                a = _smoothstep01((signed + offset_m) / span) * strength
                a = np.where((signed >= -offset_m) & (signed <= inset_m), a, 0.0)
                a = a[..., None]
                arr[..., :3] = arr[..., :3] * (1.0 - a) + edge[None, None, :] * a
        elif feather_mode != "inset" and inset_m > 0:
            inward_m = ndimage.distance_transform_edt(valid, sampling=(dz, dx))
            a = (1.0 - _smoothstep01(inward_m / inset_m)) * strength
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
        "--no-prison-composite",
        action="store_true",
        help="BlackrockRegion: skip pasting BlackrockPrisonSurvivalZone yard into the map",
    )
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
        help="Ortho PNG stem/path (default: auto ortho_color_tiled if present; "
        "BlackrockRegion/BlackrockPrisonSurvivalZone prefer ortho_color_tiled_land)",
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
        "--pipeline",
        type=str,
        default="charcoal",
        choices=["charcoal", "color"],
        help=(
            "Presentation preset: charcoal = full desat + brown edge (default ship); "
            "color = vintage partial chroma + cool dim-gray edge. Shared pre-post either way."
        ),
    )
    ap.add_argument(
        "--no-ship-post",
        action="store_true",
        help="Skip raw post-process (desaturate/levels/color/grid/stipple/vignette/grain)",
    )
    ap.add_argument(
        "--rock-textures",
        type=Path,
        default=DEFAULT_ROCK_TEXTURE_DIR,
        help=f"Folder of TLD rock albedo PNGs for enrichment fill (default {DEFAULT_ROCK_TEXTURE_DIR})",
    )
    ap.add_argument(
        "--no-rock-textures",
        action="store_true",
        help="Use flat gray rock fill instead of tiled TLD albedos",
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
        "--edge-fade-black-m",
        type=float,
        default=300.0,
        help="After the paper lift, ramp void to clean black over N meters (0=off); default 300",
    )
    ap.add_argument(
        "--edge-fade-dist-blur-m",
        type=float,
        default=DEFAULT_EDGE_FADE_DIST_BLUR_M,
        help=(
            "Gaussian blur (metres) on signed void distance to soften medial-axis seams "
            f"(0=sharp EDT); default {DEFAULT_EDGE_FADE_DIST_BLUR_M:g}"
        ),
    )
    ap.add_argument(
        "--edge-fade-black-previews",
        type=str,
        default="",
        help="Comma-separated black_m values; also write map_bg_*_black{N}m.png variants (includes 0=current)",
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

    pipeline = str(args.pipeline)
    if pipeline == "color":
        if args.style != "raw":
            print("pipeline=color forces --style raw")
            args.style = "raw"
        outside_rgb = COLOR_OUTSIDE_RGB
        mask_far_rgb = COLOR_MASK_FAR_RGB
        post_levels = COLOR_LEVELS
        post_balance = COLOR_COLOR_BALANCE
        chroma_keep: float | None = COLOR_CHROMA_KEEP
    else:
        outside_rgb = SHIP_OUTSIDE_RGB
        mask_far_rgb = SHIP_MASK_FAR_RGB
        post_levels = SHIP_LEVELS
        post_balance = SHIP_COLOR_BALANCE
        chroma_keep = None
    print(
        f"pipeline={pipeline} outside={outside_rgb} far={mask_far_rgb}"
        + (f" chroma_keep={chroma_keep}" if chroma_keep is not None else " desat=full")
    )

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
    enrich_rock_kinds: np.ndarray | None = None
    enrich_rock_kind_labels: list[str] | None = None
    enrich_rock_snow: np.ndarray | None = None
    enrich_structure_fill: np.ndarray | None = None
    enrich_h_for_punch: np.ndarray | None = None
    enrich_class_for_punch: np.ndarray | None = None
    dem_pre_enrich: np.ndarray | None = None
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
            dem_pre_enrich = dem.copy()
            dem = np.where(both, np.maximum(dem, edem), dem)

            scale = max(1.0, float(max(width, height)) / 2048.0)
            classes = emeta.get("classes")
            if classes is not None:
                eclass, cvalid = warp_label_to_image(classes, emeta, A, extent, width, height)
                enrich_class_for_punch = np.where(cvalid, eclass.astype(np.int16), np.int16(-1))
                labels = emeta.get("classLabels")
                rock_ids = enrichment_rock_ids(labels)
                struct_ids = enrichment_structure_ids(labels)
                transport_ids = enrichment_transport_ids(labels)
                protect_ids_for_punch = struct_ids
                rock_ids_for_punch = rock_ids
                overlay_ids = rock_ids | struct_ids
                rock = cvalid & np.isin(eclass, list(rock_ids))
                structure = cvalid & np.isin(eclass, list(struct_ids))
                transport = (
                    cvalid & np.isin(eclass, list(transport_ids))
                    if transport_ids
                    else np.zeros_like(cvalid, dtype=bool)
                )
                overlay = rock | structure
                protrude = both & (delta > ENRICH_PROTRUDE_M)
                # Roads/paths/rails are often flush with DEM — suppress on class, not protrude.
                enrich_cover = (overlay & protrude) | transport
                # Full rock class fill over ortho (top-hit rocks win visually).
                enrich_rock_fill = rock.copy()
                # Structure + transport: block soft rock rim (no color fill for transport).
                enrich_structure_fill = structure | transport
                # Rim rock/structure that protrude inside DEM, or sit outside primary footprint.
                # Transport rim on full class footprint (flush with DEM).
                outside = evalid & ~valid
                rock_rim_cover = rock & (protrude | outside)
                struct_rim_cover = (structure & (protrude | outside)) | transport
                enrich_outline = enrichment_rim_mask(rock_rim_cover, scale=scale)
                enrich_outline = strip_rock_rim_at_structure(
                    enrich_outline, structure | transport, scale=scale
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
                    f"transport_contour_rim={int(transport.sum())} "
                    f"rock_fill={int(enrich_rock_fill.sum())} "
                    f"rock_rim={int(enrich_outline.sum())} "
                    f"structure_rim={int(enrich_outline_structure.sum())} "
                    f"[{', '.join(hist)}] "
                    f"Y={np.nanmin(emeters):.1f}..{np.nanmax(emeters):.1f}"
                )
                rock_kinds = emeta.get("rockKinds")
                rock_snow = emeta.get("rockSnow")
                if rock_kinds is not None:
                    rk, rk_valid = warp_label_to_image(
                        rock_kinds, emeta, A, extent, width, height
                    )
                    enrich_rock_kinds = np.where(rk_valid, rk.astype(np.uint8), np.uint8(0))
                    enrich_rock_kind_labels = [
                        str(x) for x in (emeta.get("rockKindLabels") or [])
                    ]
                    rk_labels = enrich_rock_kind_labels
                    kind_hist = []
                    flat_k = rk[rk_valid & rock]
                    for i, name in enumerate(rk_labels):
                        if i == 0:
                            continue
                        n = int(np.count_nonzero(flat_k == i))
                        if n:
                            kind_hist.append(f"{name}={n}")
                    snow_n = 0
                    if rock_snow is not None:
                        rs, rs_valid = warp_label_to_image(
                            rock_snow, emeta, A, extent, width, height
                        )
                        enrich_rock_snow = np.where(
                            rs_valid, (rs != 0).astype(np.uint8), np.uint8(0)
                        )
                        snow_n = int(np.count_nonzero(rs_valid & rock & (rs != 0)))
                    print(
                        f"  rockKinds [{', '.join(kind_hist) or 'none'}] "
                        f"rockSnow={snow_n}"
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

    # BlackrockRegion precision rule: paste prison survival-zone yard into outdoor map UV.
    prison_paste = None
    scene = load_json(dump / "meta.json").get("sceneName") or dump.name
    if scene == "BlackrockRegion" and not args.no_prison_composite:
        from blackrock_prison_composite import try_apply_prison_paste

        repo_root = Path(__file__).resolve().parent.parent
        dem_base = dem_pre_enrich if dem_pre_enrich is not None else dem
        prison_paste = try_apply_prison_paste(
            outdoor_dump=dump,
            repo_root=repo_root,
            A=A,
            extent=extent,
            width=width,
            height=height,
            dem_outdoor=dem_base,
            valid_outdoor=valid,
            display_valid=display_valid,
        )
        if prison_paste is not None:
            dem = prison_paste.dem
            valid = prison_paste.valid
            display_valid = prison_paste.display_valid
            enrich_cover = prison_paste.enrich_cover
            enrich_outline = prison_paste.enrich_outline
            enrich_outline_structure = prison_paste.enrich_outline_structure
            enrich_rock_fill = prison_paste.enrich_rock_fill
            enrich_structure_fill = prison_paste.enrich_structure_fill
            if prison_paste.enrich_rock_kinds is not None:
                enrich_rock_kinds = prison_paste.enrich_rock_kinds
            if prison_paste.enrich_rock_snow is not None:
                enrich_rock_snow = prison_paste.enrich_rock_snow
            if prison_paste.enrich_rock_kind_labels is not None:
                enrich_rock_kind_labels = prison_paste.enrich_rock_kind_labels
            if prison_paste.enrich_h_for_punch is not None:
                enrich_h_for_punch = prison_paste.enrich_h_for_punch
            if prison_paste.enrich_class_for_punch is not None:
                enrich_class_for_punch = prison_paste.enrich_class_for_punch
            # Punch protect ids from outdoor labels still apply.
            print("Blackrock prison yard paste applied")
    elif scene == "BlackrockRegion" and args.no_prison_composite:
        print("Blackrock prison paste: skipped (--no-prison-composite)")

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
        if enrich_rock_kinds is not None:
            enrich_rock_kinds = np.where(~cut & display_valid, enrich_rock_kinds, np.uint8(0))
        if enrich_rock_snow is not None:
            enrich_rock_snow = np.where(~cut & display_valid, enrich_rock_snow, np.uint8(0))
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
    ortho_paths = resolve_ortho_paths(dump, args.ortho, scene=str(scene) if scene else None)
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
        if prison_paste is not None and owarp is not None and ovalid is not None:
            from blackrock_prison_composite import blend_ortho_with_prison

            owarp, ovalid = blend_ortho_with_prison(
                owarp,
                ovalid,
                prison_dump=prison_paste.prison_dump,
                A=A,
                extent=extent,
                width=width,
                height=height,
                alpha=prison_paste.alpha,
            )
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

    ortho_repost_mask = concentrator_ortho_repost_mask(
        scene=str(scene),
        A=A,
        extent=extent,
        width=width,
        height=height,
        structure=enrich_structure_fill,
        ortho_valid=ovalid,
    )
    dem_ortho_repost: np.ndarray | None = None
    if ortho_repost_mask is not None and dem_pre_enrich is not None:
        # Shade with terrain + structure heights only — drop under-roof rock bumps.
        dem_ortho_repost = dem_pre_enrich.copy()
        if enrich_structure_fill is not None:
            lift = ortho_repost_mask & enrich_structure_fill & np.isfinite(dem)
            dem_ortho_repost = np.where(lift, dem, dem_ortho_repost)
    if ortho_repost_mask is not None and enrich_rock_fill is not None:
        cleared = enrich_rock_fill & ortho_repost_mask
        if cleared.any():
            enrich_rock_fill = enrich_rock_fill & ~ortho_repost_mask
            if enrich_rock_kinds is not None:
                enrich_rock_kinds = np.where(ortho_repost_mask, np.uint8(0), enrich_rock_kinds)
            if enrich_rock_snow is not None:
                enrich_rock_snow = np.where(ortho_repost_mask, np.uint8(0), enrich_rock_snow)
            print(f"Concentrator: cleared rock fill under ortho repost: {int(cleared.sum())} px")
    if ortho_repost_mask is not None and enrich_outline is not None:
        # Drop under-roof rock rims inside the footprint.
        rim_cleared = enrich_outline & ortho_repost_mask
        if rim_cleared.any():
            enrich_outline = enrich_outline & ~ortho_repost_mask
            print(f"Concentrator: cleared rock rim under ortho repost: {int(rim_cleared.sum())} px")

    if args.style == "raw":
        rock_bank: dict[str, np.ndarray] = {}
        if not args.no_rock_textures:
            rock_bank = load_rock_texture_bank(args.rock_textures)
            if rock_bank:
                print(f"rock textures: {len(rock_bank)} from {args.rock_textures}")
            else:
                print(f"rock textures: none in {args.rock_textures} (flat fill)")
        else:
            print("rock textures: off (--no-rock-textures)")
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
            enrich_rock_kinds=enrich_rock_kinds,
            enrich_rock_kind_labels=enrich_rock_kind_labels,
            enrich_rock_snow=enrich_rock_snow,
            rock_texture_bank=rock_bank,
            enrich_structure_fill=enrich_structure_fill,
            ortho_repost_mask=ortho_repost_mask,
            dem_ortho_repost=dem_ortho_repost,
            shade_floor=args.shade_floor,
            outside_rgb=outside_rgb,
        )
        print(f"style=raw contour_m={args.contour_m} shade_floor={args.shade_floor}")
        pre_shade = img.copy()
        feather_zone = excluded if args.edge_feather_mode == "inset" else None
        black_preview_ms: list[float] = []
        if args.edge_fade_black_previews.strip():
            for part in args.edge_fade_black_previews.split(","):
                part = part.strip()
                if part:
                    black_preview_ms.append(float(part))

        def apply_edge_fade(src: Image.Image, black_m: float) -> Image.Image:
            return shade_outside(
                src,
                valid,
                A,
                extent,
                fade_m=args.edge_fade_m,
                lift=args.edge_fade_lift,
                feather_m=args.edge_feather_m,
                feather_strength=args.edge_feather_strength,
                feather_mode=args.edge_feather_mode,
                feather_zone=feather_zone,
                feather_offset_m=args.edge_feather_offset_m,
                black_m=black_m,
                dist_blur_m=float(args.edge_fade_dist_blur_m),
                outside_rgb=outside_rgb,
                mask_far_rgb=mask_far_rgb,
            )

        primary_black_m = float(args.edge_fade_black_m)
        img = apply_edge_fade(pre_shade, primary_black_m)
        print(
            f"edge fade: {args.edge_fade_m:g} m lift={args.edge_fade_lift:g} "
            f"black={primary_black_m:g} m dist_blur={float(args.edge_fade_dist_blur_m):g} m "
            f"feather inset={args.edge_feather_m:g} m offset={args.edge_feather_offset_m:g} m "
            f"@{args.edge_feather_strength:g} mode={args.edge_feather_mode}"
        )
        void_texture_variants: list[tuple[str, Image.Image]] | None = None
        edge_black_variants: list[tuple[str, Image.Image]] = []

        def run_ship_post(src: Image.Image, black_m: float) -> Image.Image:
            grid_m = 0.0 if args.no_grid else float(args.grid_m)
            grain_sigma = 0.0 if args.no_grain else float(args.grain_sigma)
            vig = 0.0 if args.no_vignette else float(args.vignette)
            out = postprocess_ship_look(
                src,
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
                levels_params=post_levels,
                color_balance_params=post_balance,
                chroma_keep=chroma_keep,
            )
            # Re-assert pure black in the deep void so grain/stipple/grid do not persist.
            if black_m > 0:
                w = void_black_weight(
                    valid,
                    A,
                    extent,
                    fade_m=args.edge_fade_m,
                    black_m=black_m,
                    dist_blur_m=float(args.edge_fade_dist_blur_m),
                )
                out = apply_clean_void_black(out, w)
            return out

        if not args.no_ship_post:
            img = run_ship_post(img, primary_black_m)
            print(
                f"ship-post: desat={not args.no_desaturate} levels={not args.no_levels} "
                f"color_balance={not args.no_color_balance} "
                f"chroma_keep={chroma_keep if chroma_keep is not None else 'full'} "
                f"grid_m={0.0 if args.no_grid else float(args.grid_m)} "
                f"grid_width={args.grid_width} stipple={not args.no_stipple} "
                f"vignette={0.0 if args.no_vignette else float(args.vignette)} "
                f"grain_sigma={0.0 if args.no_grain else float(args.grain_sigma)}"
                + (f" clean_void_black={primary_black_m:g}m" if primary_black_m > 0 else "")
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
            for bm in black_preview_ms:
                if abs(bm - primary_black_m) < 1e-6:
                    continue
                faded = apply_edge_fade(pre_shade, bm)
                edge_black_variants.append((f"black{bm:g}m", run_ship_post(faded, bm)))
                print(f"edge-fade-black preview: {bm:g} m (clean void)")
        else:
            print("ship-post: off")
            if args.void_texture_tests:
                print("void-texture-tests skipped (--no-ship-post)")
            for bm in black_preview_ms:
                if abs(bm - primary_black_m) < 1e-6:
                    continue
                edge_black_variants.append((f"black{bm:g}m", apply_edge_fade(pre_shade, bm)))
                print(f"edge-fade-black preview: {bm:g} m")
    else:
        void_texture_variants = None
        edge_black_variants = []
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

    if edge_black_variants:
        for name, variant in edge_black_variants:
            vpath = out.with_name(f"{out.stem}_{name}.png")
            variant.save(vpath)
            print(f"wrote {vpath}")

    if args.baseline and args.baseline.exists():
        cmp = out.with_name(out.stem + "_vs_baseline.png")
        make_side_by_side(img, Image.open(args.baseline), cmp)
        print(f"wrote comparison {cmp}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
