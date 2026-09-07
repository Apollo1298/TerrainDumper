#!/usr/bin/env python3
"""Render a top-down world-space template for hand-painting a region exclusion mask.

Regions contain terrain that is rendered and walkable-looking but out of bounds (most
visibly CoastalRegion's ocean sheet). Nothing in the game data marks that boundary, so it
is painted by hand once per region and kept in the repo under masks/<Scene>/.

Usage:
  python tools/make_mask_template.py <dump_dir> [--meters-per-pixel 2] [--out-dir masks]

Default is 2 m/px. Writes:
  masks/<Scene>/template.png   — ortho×hillshade paint guide when ortho is present
                                 (else DEM hillshade); north up, world grid
  masks/<Scene>/mask.json      — world bounds + pixel size sidecar

When masks/<Scene>/mask.json already exists, regen reuses its world AABB,
metersPerPixel, and width/height so painted mask.png stays aligned. Pass
--recompute-bounds to derive a fresh footprint (may change size).

Footprint AABB (new scenes / --recompute-bounds) defaults to the primary land
tile plus same-res land siblings, then expands to cover the fog UV world
rectangle (vanilla map canvas) so painted masks can reach the left/right edges
of non-square maps (e.g. Ravine 2:1). Water/ice sheets and low-res overlays do
not widen the DEM crop; fog UV may. Within that crop, ortho texture may still
fill land-DEM holes (paint guide only).

Then, in any image editor: paint the areas to exclude on a new layer, hide the template
layer, and export just the painted layer as masks/<Scene>/mask.png at the same pixel size.
Anything opaque (or non-black if the export has no alpha) counts as excluded.
make_map_bg.py requires that mask by region name on the next build.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mapalign import (  # noqa: E402
    _is_backdrop_tile,
    apply_affine,
    enrichment_rock_ids,
    enrichment_structure_ids,
    fit_affine,
    invert_affine,
    list_terrain_stems,
    load_enrichment,
    load_json,
    load_tile_heights,
    main_tile_stem,
    resolve_map_extent,
)
from make_map_bg import (  # noqa: E402
    DEFAULT_ROCK_TEXTURE_DIR,
    PUNCH_WATER_DARK_LUMA,
    PUNCH_WATER_DEEP_MAX_Y,
    PUNCH_WATER_NEAR_M,
    PUNCH_WATER_SHRINK_M,
    SHIP_OUTSIDE_RGB,
    hillshade,
    load_rock_texture_bank,
    paint_enrichment_rock_textures,
    punch_dark_ortho_over_elevated_enrichment,
    resolve_ortho_paths,
)

# Row 0 of the template is max Z so north is up, matching how the finished map reads.
ROW_ORDER = "row 0 = maxZ (north up)"
# Match make_map_bg raw-style hillshade lift so shadows stay readable without crushing color.
SHADE_FLOOR = 0.18


def _res_close(a: int, b: int) -> bool:
    """True when heightmap resolutions look like grid siblings (Ash Canyon 4x4)."""
    if a <= 0 or b <= 0:
        return False
    return abs(a - b) <= max(1, int(0.1 * max(a, b)))


def fog_uv_world_aabb(dump: Path) -> tuple[float, float, float, float] | None:
    """World XZ AABB of the fog UV map rectangle, or None if fog/alignment missing."""
    samples_path = dump / "alignment_samples.json"
    if not samples_path.is_file():
        return None
    try:
        extent = resolve_map_extent(dump, use_sample_extent=False)
    except ValueError:
        return None
    samples = load_json(samples_path)
    pts = list(samples.get("samples") or [])
    if len(pts) < 3:
        return None
    W = np.array([[float(s["world"]["x"]), float(s["world"]["z"])] for s in pts], dtype=np.float64)
    M = np.array([[float(s["map"]["x"]), float(s["map"]["y"])] for s in pts], dtype=np.float64)
    A = fit_affine(W, M)
    B = invert_affine(A)
    mx0, my0, mx1, my1 = extent
    corners = np.array(
        [
            [mx0, my0],
            [mx1, my0],
            [mx0, my1],
            [mx1, my1],
            [mx0, 0.5 * (my0 + my1)],
            [mx1, 0.5 * (my0 + my1)],
            [0.5 * (mx0 + mx1), my0],
            [0.5 * (mx0 + mx1), my1],
        ],
        dtype=np.float64,
    )
    world = apply_affine(B, corners)
    return (
        float(world[:, 0].min()),
        float(world[:, 1].min()),
        float(world[:, 0].max()),
        float(world[:, 1].max()),
    )


def footprint_stems(dump: Path, *, main_only: bool, all_tiles: bool) -> list[str]:
    """
    Stems that define the template world AABB (the DEM crop).

    Default = primary land tile plus same-res land siblings (Ash Canyon / Cannery
    grids). Water/ice backdrop sheets and low-res land overlays never widen the
    crop — they may still tint heights inside it.
    """
    main = main_tile_stem(dump)
    if main_only:
        return [main]
    stems = list_terrain_stems(dump)
    if all_tiles:
        return stems

    main_meta = load_json(dump / f"{main}_meta.json")
    main_res = int(main_meta.get("heightmapResolution") or 0)
    out: list[str] = []
    for stem in stems:
        meta = load_json(dump / f"{stem}_meta.json")
        if _is_backdrop_tile(meta):
            continue
        res = int(meta.get("heightmapResolution") or 0)
        if stem == main or _res_close(res, main_res):
            out.append(stem)
    return out or [main]


def load_tile_list(dump: Path, stems: list[str]) -> list[tuple[np.ndarray, dict]]:
    return [load_tile_heights(dump, stem) for stem in stems]


def sample_source(
    meters: np.ndarray, meta: dict, wx: np.ndarray, wz: np.ndarray
) -> np.ndarray:
    """Nearest-neighbour sample of a world-anchored raster; NaN outside its footprint."""
    th, tw = meters.shape
    pos, size = meta["position"], meta["size"]
    sx = (wx - float(pos["x"])) / float(size["x"]) * (tw - 1)
    sz = (wz - float(pos["z"])) / float(size["z"]) * (th - 1)
    inside = (sx >= 0) & (sx <= tw - 1) & (sz >= 0) & (sz <= th - 1)
    ix = np.clip(np.rint(sx), 0, tw - 1).astype(np.int32)
    iz = np.clip(np.rint(sz), 0, th - 1).astype(np.int32)
    return np.where(inside, meters[iz, ix], np.nan)


def sample_ortho_world(
    ortho_rgb: np.ndarray,
    ortho_meta: dict,
    wx: np.ndarray,
    wz: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """Bilinear-sample ortho into a world X/Z grid (north = +Z → image top)."""
    oh, ow = ortho_rgb.shape[0], ortho_rgb.shape[1]
    cx = float(ortho_meta["centerX"])
    cz = float(ortho_meta["centerZ"])
    half = float(ortho_meta["orthographicSize"])

    su = (wx - (cx - half)) / (2 * half) * (ow - 1)
    sv = (1.0 - (wz - (cz - half)) / (2 * half)) * (oh - 1)
    inside = (su >= 0) & (su <= ow - 1) & (sv >= 0) & (sv <= oh - 1)

    su0 = np.clip(np.floor(su).astype(np.int32), 0, ow - 1)
    sv0 = np.clip(np.floor(sv).astype(np.int32), 0, oh - 1)
    su1 = np.clip(su0 + 1, 0, ow - 1)
    sv1 = np.clip(sv0 + 1, 0, oh - 1)
    fu = np.clip(su - su0, 0, 1)[..., None]
    fv = np.clip(sv - sv0, 0, 1)[..., None]
    c00 = ortho_rgb[sv0, su0].astype(np.float64)
    c10 = ortho_rgb[sv0, su1].astype(np.float64)
    c01 = ortho_rgb[sv1, su0].astype(np.float64)
    c11 = ortho_rgb[sv1, su1].astype(np.float64)
    sampled = (1 - fu) * (1 - fv) * c00 + fu * (1 - fv) * c10 + (1 - fu) * fv * c01 + fu * fv * c11

    rgb = np.zeros(wx.shape + (3,), dtype=np.float64)
    rgb[inside] = sampled[inside]
    return rgb, inside


def draw_grid(
    img: Image.Image,
    origin_x: float,
    origin_z: float,
    max_z: float,
    mpp: float,
    minor_m: float,
    major_m: float,
) -> None:
    draw = ImageDraw.Draw(img, "RGBA")
    w, h = img.size

    def line_positions(step: float, origin: float, span_px: int) -> list[tuple[int, float]]:
        first = np.ceil(origin / step) * step
        pos = []
        v = first
        while True:
            px = int(round((v - origin) / mpp))
            if px > span_px:
                break
            if 0 <= px <= span_px:
                pos.append((px, v))
            v += step
        return pos

    for step, colour in ((minor_m, (255, 255, 255, 40)), (major_m, (255, 255, 255, 110))):
        for px, _ in line_positions(step, origin_x, w):
            draw.line([(px, 0), (px, h)], fill=colour, width=1)
        for pz, world_z in line_positions(step, origin_z, h):
            py = int(round((max_z - world_z) / mpp))
            draw.line([(0, py), (w, py)], fill=colour, width=1)

    for px, world_x in line_positions(major_m, origin_x, w):
        draw.text((px + 3, 3), f"X {world_x:.0f}", fill=(255, 255, 255, 190))
    for pz, world_z in line_positions(major_m, origin_z, h):
        py = int(round((max_z - world_z) / mpp))
        draw.text((3, py + 3), f"Z {world_z:.0f}", fill=(255, 255, 255, 190))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dump_dir", type=Path)
    ap.add_argument(
        "--meters-per-pixel",
        type=float,
        default=2.0,
        help="Template resolution in world metres per pixel (default 2)",
    )
    ap.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help="Parent masks folder (default: <repo>/masks). Writes into <out-dir>/<Scene>/",
    )
    ap.add_argument("--grid-minor-m", type=float, default=100.0)
    ap.add_argument("--grid-major-m", type=float, default=500.0)
    ap.add_argument(
        "--all-tiles",
        action="store_true",
        help="Expand footprint AABB to every terrain tile (includes low-res overlays)",
    )
    ap.add_argument(
        "--main-tile-only",
        action="store_true",
        help="Footprint = primary land tile only (no water sheets / grid siblings)",
    )
    ap.add_argument(
        "--include-portals",
        action="store_true",
        help="Expand AABB to cover portals.json LoadScene positions (beyond land DEM)",
    )
    ap.add_argument(
        "--portal-pad-m",
        type=float,
        default=50.0,
        help="Padding metres when --include-portals (default 50)",
    )
    ap.add_argument(
        "--no-fog-uv",
        action="store_true",
        help="Do not expand AABB to the fog UV / vanilla map canvas (DEM footprint only)",
    )
    ap.add_argument("--inset-m", type=float, default=None, help="Match make_map_bg --inset-m")
    ap.add_argument("--inset-frac", type=float, default=None, help="Match make_map_bg --inset-frac")
    ap.add_argument(
        "--ortho",
        default="",
        help="Ortho PNG stem/path (default: auto; BlackrockRegion/BlackrockPrisonSurvivalZone prefer ortho_color_tiled_land)",
    )
    ap.add_argument(
        "--shade-floor",
        type=float,
        default=SHADE_FLOOR,
        help=f"Hillshade lift 0..0.95 when tinting ortho (default {SHADE_FLOOR})",
    )
    ap.add_argument(
        "--no-ortho",
        action="store_true",
        help="Force DEM hillshade only (ignore ortho even if present)",
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
        "--rock-textures",
        type=Path,
        default=DEFAULT_ROCK_TEXTURE_DIR,
        help=f"Folder of TLD rock albedo PNGs for enrichment fill (default {DEFAULT_ROCK_TEXTURE_DIR})",
    )
    ap.add_argument(
        "--no-rock-textures",
        action="store_true",
        help="Paint enrichment rocks as flat gray (skip TLD albedo tiles)",
    )
    ap.add_argument(
        "--recompute-bounds",
        action="store_true",
        help="Ignore existing mask.json size; derive a fresh AABB (may break painted masks)",
    )
    args = ap.parse_args()

    dump: Path = args.dump_dir
    if not dump.is_dir():
        print(f"No dump directory at {dump}")
        return 1

    scene = dump.name
    meta_path = dump / "meta.json"
    if meta_path.exists():
        scene = load_json(meta_path).get("sceneName") or scene

    masks_root: Path = args.out_dir or (Path(__file__).resolve().parent.parent / "masks")
    out_dir = masks_root / scene
    sidecar_path = out_dir / "mask.json"

    fp_stems = footprint_stems(
        dump, main_only=bool(args.main_tile_only), all_tiles=bool(args.all_tiles)
    )
    fp_sources = load_tile_list(dump, fp_stems)
    if not fp_sources:
        print(f"No terrain tiles in {dump}")
        return 1

    locked_bounds = False
    if not args.recompute_bounds and sidecar_path.is_file():
        try:
            prev = load_json(sidecar_path)
            min_x = float(prev["originX"])
            min_z = float(prev["originZ"])
            max_x = float(prev["maxX"])
            max_z = float(prev["maxZ"])
            mpp = float(prev["metersPerPixel"])
            width = max(2, int(prev["width"]))
            height = max(2, int(prev["height"]))
            locked_bounds = True
            if abs(float(args.meters_per_pixel) - mpp) > 1e-9:
                print(
                    f"locked bounds: ignoring --meters-per-pixel {args.meters_per_pixel:g} "
                    f"(sidecar uses {mpp:g}; pass --recompute-bounds to change)"
                )
            print(
                f"{scene}: locked to existing mask.json "
                f"X[{min_x:.0f},{max_x:.0f}] Z[{min_z:.0f},{max_z:.0f}] -> "
                f"{width}x{height} @ {mpp} m/px (footprint {', '.join(fp_stems)})"
            )
        except (KeyError, TypeError, ValueError, OSError) as ex:
            print(f"existing mask.json unusable ({ex}); recomputing bounds")

    if not locked_bounds:
        min_x = min(float(m["position"]["x"]) for _, m in fp_sources)
        min_z = min(float(m["position"]["z"]) for _, m in fp_sources)
        max_x = max(float(m["position"]["x"]) + float(m["size"]["x"]) for _, m in fp_sources)
        max_z = max(float(m["position"]["z"]) + float(m["size"]["z"]) for _, m in fp_sources)

        if args.include_portals:
            portals_path = dump / "portals.json"
            if not portals_path.is_file():
                print(f"--include-portals: no portals.json in {dump}")
                return 1
            portals_doc = load_json(portals_path)
            portals = list(portals_doc.get("portals") or [])
            if not portals:
                print("--include-portals: portals.json has no portals[]")
                return 1
            pad = float(args.portal_pad_m)
            px = [float(p["x"]) for p in portals]
            pz = [float(p["z"]) for p in portals]
            before = (min_x, min_z, max_x, max_z)
            min_x = min(min_x, min(px) - pad)
            min_z = min(min_z, min(pz) - pad)
            max_x = max(max_x, max(px) + pad)
            max_z = max(max_z, max(pz) + pad)
            print(
                f"expanded AABB for {len(portals)} portals (+{pad:g}m pad): "
                f"X[{before[0]:.0f},{before[2]:.0f}] Z[{before[1]:.0f},{before[3]:.0f}] -> "
                f"X[{min_x:.0f},{max_x:.0f}] Z[{min_z:.0f},{max_z:.0f}]"
            )

        if not args.no_fog_uv:
            fog_aabb = fog_uv_world_aabb(dump)
            if fog_aabb is None:
                print("fog UV expand skipped (need fog_of_war.json + alignment_samples.json)")
            else:
                fx0, fz0, fx1, fz1 = fog_aabb
                before = (min_x, min_z, max_x, max_z)
                min_x = min(min_x, fx0)
                min_z = min(min_z, fz0)
                max_x = max(max_x, fx1)
                max_z = max(max_z, fz1)
                if (min_x, min_z, max_x, max_z) != before:
                    print(
                        f"expanded AABB to fog UV canvas: "
                        f"X[{before[0]:.0f},{before[2]:.0f}] Z[{before[1]:.0f},{before[3]:.0f}] -> "
                        f"X[{min_x:.0f},{max_x:.0f}] Z[{min_z:.0f},{max_z:.0f}]"
                    )
                else:
                    print("fog UV canvas already inside DEM footprint")

        mpp = float(args.meters_per_pixel)
        width = max(2, int(np.ceil((max_x - min_x) / mpp)))
        height = max(2, int(np.ceil((max_z - min_z) / mpp)))
        print(
            f"{scene}: X[{min_x:.0f},{max_x:.0f}] Z[{min_z:.0f},{max_z:.0f}] -> "
            f"{width}x{height} @ {mpp} m/px (footprint {', '.join(fp_stems)})"
        )

    wx = min_x + (np.arange(width) + 0.5) * mpp
    # Row 0 is max Z so the template reads north-up.
    wz = max_z - (np.arange(height) + 0.5) * mpp
    wx_grid, wz_grid = np.meshgrid(wx, wz)

    # Heights: footprint tiles define valid. Other tiles (overlays / water) may
    # refine heights inside that crop but must not punch new valid pixels.
    dem = np.full((height, width), np.nan)
    for meters, meta in fp_sources:
        sampled = sample_source(meters, meta, wx_grid, wz_grid)
        dem = np.fmax(dem, sampled)

    valid = np.isfinite(dem)
    if not valid.any():
        print("No terrain sampled into the template")
        return 1

    if not args.main_tile_only:
        fp_set = set(fp_stems)
        for stem in list_terrain_stems(dump):
            if stem in fp_set:
                continue
            meters, meta = load_tile_heights(dump, stem)
            sampled = sample_source(meters, meta, wx_grid, wz_grid)
            dem = np.where(valid & np.isfinite(sampled), np.fmax(dem, sampled), dem)

    # Enrichment adds detail but must not widen the footprint — same rule as make_map_bg.
    enrich_h: np.ndarray | None = None
    enrich_class: np.ndarray | None = None
    protect_ids: set[int] | None = None
    rock_ids: set[int] | None = None
    enrich_rock_kinds: np.ndarray | None = None
    enrich_rock_snow: np.ndarray | None = None
    enrich_rock_kind_labels: list[str] | None = None
    loaded = load_enrichment(dump)
    if loaded is not None:
        emeters, _emask, emeta = loaded
        edem = sample_source(emeters, emeta, wx_grid, wz_grid)
        enrich_h = edem  # keep pre-merge heights for water-ortho punch
        dem = np.where(valid & np.isfinite(edem), np.fmax(dem, edem), dem)
        classes = emeta.get("classes")
        if classes is not None:
            # Nearest-neighbour class ids onto the template grid.
            cls = sample_source(classes.astype(np.float64), emeta, wx_grid, wz_grid)
            enrich_class = np.full(cls.shape, -1, dtype=np.int32)
            finite = np.isfinite(cls)
            enrich_class[finite] = np.rint(cls[finite]).astype(np.int32)
            labels = emeta.get("classLabels")
            protect_ids = enrichment_structure_ids(labels)
            rock_ids = enrichment_rock_ids(labels)
        rock_kinds = emeta.get("rockKinds")
        if rock_kinds is not None:
            rk = sample_source(rock_kinds.astype(np.float64), emeta, wx_grid, wz_grid)
            enrich_rock_kinds = np.zeros(rk.shape, dtype=np.uint8)
            rk_ok = np.isfinite(rk)
            enrich_rock_kinds[rk_ok] = np.rint(rk[rk_ok]).astype(np.uint8)
            enrich_rock_kind_labels = [str(x) for x in (emeta.get("rockKindLabels") or [])]
        rock_snow = emeta.get("rockSnow")
        if rock_snow is not None:
            rs = sample_source(rock_snow.astype(np.float64), emeta, wx_grid, wz_grid)
            enrich_rock_snow = np.zeros(rs.shape, dtype=np.uint8)
            rs_ok = np.isfinite(rs)
            enrich_rock_snow[rs_ok] = (rs[rs_ok] != 0).astype(np.uint8)
        print("merged enrichment inside footprint")

    rock_bank: dict[str, np.ndarray] = {}
    if not args.no_rock_textures:
        rock_bank = load_rock_texture_bank(args.rock_textures)
        if rock_bank:
            print(f"rock textures: {len(rock_bank)} from {args.rock_textures}")
        else:
            print(f"rock textures: none in {args.rock_textures} (flat fill)")
    else:
        print("rock textures: off (--no-rock-textures)")

    inset_m = 0.0
    if args.inset_m is not None:
        inset_m = float(args.inset_m)
    elif args.inset_frac:
        side = min(
            min(float(m["size"]["x"]) for _, m in fp_sources),
            min(float(m["size"]["z"]) for _, m in fp_sources),
        )
        inset_m = float(args.inset_frac) * side
    if inset_m > 0:
        from scipy import ndimage

        dist_m = ndimage.distance_transform_edt(valid, sampling=(mpp, mpp))
        valid = valid & (dist_m >= inset_m)
        print(f"inset {inset_m:.1f} m")
    print(f"coverage: {valid.mean()*100:.1f}%")

    dem_safe = np.where(valid, dem, 0.0)
    shade = hillshade(dem_safe, valid)
    floor = float(np.clip(args.shade_floor, 0.0, 0.95))
    shade_soft = floor + shade * (1.0 - floor)

    ortho_paths = None if args.no_ortho else resolve_ortho_paths(dump, args.ortho, scene=scene)
    if ortho_paths is not None:
        ortho_path, meta_path = ortho_paths
        orgb = np.asarray(Image.open(ortho_path).convert("RGB"), dtype=np.uint8)
        ometa = load_json(meta_path)
        owarp, ovalid = sample_ortho_world(orgb, ometa, wx_grid, wz_grid)
        if not args.no_punch_water_ortho and enrich_h is not None:
            owarp, ovalid, n_punch = punch_dark_ortho_over_elevated_enrichment(
                owarp,
                ovalid,
                enrich_h,
                deep_max_y=float(args.punch_deep_max_y),
                dark_luma_max=float(args.punch_dark_luma),
                near_m=float(args.punch_near_m),
                shrink_m=float(args.punch_shrink_m),
                meters_per_pixel=mpp,
                enrichment_class=enrich_class,
                protect_class_ids=protect_ids,
                rock_class_ids=rock_ids,
            )
            print(
                f"inpainted water-over-rock ortho: {n_punch} px "
                f"(deep_max_y={args.punch_deep_max_y:g}, dark_luma={args.punch_dark_luma:g}, "
                f"near_m={args.punch_near_m:g}, shrink_m={args.punch_shrink_m:g})"
            )
        out = owarp.copy()
        use = valid & ovalid
        for c in range(3):
            out[..., c] = np.where(use, out[..., c] * shade_soft, out[..., c])
        dem_only = valid & ~ovalid
        gray = shade_soft * 255.0
        for c in range(3):
            out[..., c] = np.where(dem_only, gray, out[..., c])
        # Paint guide: keep ortho where land DEM is missing (e.g. Cannery NE hole)
        # but the tiled ortho still has texture. Only blank true voids.
        show = valid | ovalid
        ortho_fill = ovalid & ~valid
        out[~show] = SHIP_OUTSIDE_RGB
        # Match make_map_bg raw/color pre-post: textured enrichment rocks over ortho.
        if enrich_class is not None and rock_ids:
            rock_on = show & np.isin(enrich_class, list(rock_ids))
            if rock_on.any():
                status = paint_enrichment_rock_textures(
                    out,
                    rock_on,
                    shade_soft,
                    rock_kinds=enrich_rock_kinds,
                    rock_kind_labels=enrich_rock_kind_labels,
                    rock_snow=enrich_rock_snow,
                    texture_bank=rock_bank,
                )
                print(f"enrichment rock fill over ortho: {status}")
        print(
            f"ortho x hillshade {ortho_path.name} "
            f"(coverage {use.mean()*100:.1f}%, ortho-fill {ortho_fill.mean()*100:.1f}%, "
            f"shade_floor={floor:g})"
        )
        rgb = np.clip(out, 0, 255)
    else:
        if args.ortho and not args.no_ortho:
            print(f"Ortho missing for --ortho {args.ortho!r} — DEM hillshade fallback")
        elif not args.no_ortho:
            print("No ortho_color_tiled found — DEM hillshade fallback")
        z = np.where(valid, dem, np.nan)
        lo, hi = np.nanpercentile(z, 2), np.nanpercentile(z, 98)
        tone = np.clip((z - lo) / max(1e-6, hi - lo), 0, 1)
        grey = np.clip((0.35 * tone + 0.65 * shade) * 235 + 10, 0, 255)
        rgb = np.repeat(np.nan_to_num(grey)[..., None], 3, axis=2)
        rgb[~valid] = SHIP_OUTSIDE_RGB
        if enrich_class is not None and rock_ids:
            rock_on = valid & np.isin(enrich_class, list(rock_ids))
            if rock_on.any():
                status = paint_enrichment_rock_textures(
                    rgb,
                    rock_on,
                    shade_soft,
                    rock_kinds=enrich_rock_kinds,
                    rock_kind_labels=enrich_rock_kind_labels,
                    rock_snow=enrich_rock_snow,
                    texture_bank=rock_bank,
                )
                print(f"enrichment rock fill over hillshade: {status}")

    img = Image.fromarray(rgb.astype(np.uint8), "RGB")

    draw_grid(img, min_x, min_z, max_z, mpp, args.grid_minor_m, args.grid_major_m)

    out_dir.mkdir(parents=True, exist_ok=True)
    template_path = out_dir / "template.png"
    paint_path = out_dir / "mask.png"
    img.save(template_path)
    sidecar_path.write_text(
        json.dumps(
            {
                "sceneName": scene,
                "originX": min_x,
                "originZ": min_z,
                "maxX": max_x,
                "maxZ": max_z,
                "metersPerPixel": mpp,
                "width": width,
                "height": height,
                "rowOrder": ROW_ORDER,
                "maskFile": "mask.png",
                "notes": [
                    "Paint areas to EXCLUDE from the map, then export that layer alone.",
                    "Opaque (or non-black without alpha) = excluded.",
                    "Template prefers ortho_color_tiled x hillshade when present in the dump "
                    "(BlackrockRegion / BlackrockPrisonSurvivalZone prefer ortho_color_tiled_land).",
                    "Enrichment rockKinds/rockSnow paint TLD albedo tiles (same as color map pre-post).",
                    "Regen reuses this sidecar AABB/size unless --recompute-bounds.",
                    "Footprint AABB = primary land DEM (+ same-res land siblings), then fog UV canvas.",
                    "Water/ice do not widen the DEM crop; fog UV may expand past it (vanilla aspect).",
                    "Within that crop, ortho pixels may fill land-DEM holes (paint guide only).",
                    "Optional --include-portals expands AABB past DEM to cover LoadScene markers.",
                    "Optional --no-fog-uv keeps DEM footprint only.",
                    "Mask must match width/height above; make_map_bg.py loads masks/<Scene>/mask.png.",
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"template -> {template_path}")
    print(f"sidecar  -> {sidecar_path}")
    print(f"paint    -> {paint_path}")
    if paint_path.is_file():
        try:
            mw, mh = Image.open(paint_path).size
            if (mw, mh) != (width, height):
                print(
                    f"WARNING: existing mask.png is {mw}x{mh} but template is now "
                    f"{width}x{height} — re-export mask.png after painting"
                )
        except OSError as ex:
            print(f"WARNING: could not check mask.png size: {ex}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
