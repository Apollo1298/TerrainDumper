"""Paste BlackrockPrisonSurvivalZone into BlackrockRegion hole, then raw-render.

Uses tools/blackrock_prison_composite.py (same precision rule as make_map_bg).
Default output is raw color (no ship-post) for quick checks.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
from scipy import ndimage

ROOT = Path(__file__).resolve().parents[1]
TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))

import make_map_bg as mm  # noqa: E402
import blackrock_prison_composite as bpc  # noqa: E402
from tld_paths import require_dump_root  # noqa: E402
from mapalign import (  # noqa: E402
    collect_samples,
    fit_affine,
    list_terrain_stems,
    load_json,
    load_tile_heights,
    main_tile_stem,
    output_size_for_extent,
    resolve_map_extent,
    warp_dem_to_image,
    warp_ortho_to_image,
)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--dump-root",
        type=Path,
        default=None,
        help="Dump folder (default: $TERRAIN_DUMPER_ROOT or $TLD_PATH/Mods/TerrainDumper)",
    )
    ap.add_argument("--size", type=int, default=4096)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--no-mask", action="store_true", help="Skip BlackrockRegion exclusion mask")
    ap.add_argument(
        "--full-dem-hole",
        action="store_true",
        help="Paste over entire prison DEM tile (ignore painted yard mask)",
    )
    ap.add_argument(
        "--yard-mask",
        type=Path,
        default=None,
        help="Prison exclusion mask; KEEP region (not painted) is the paste hole "
        "(default: masks/BlackrockPrisonSurvivalZone/mask.png)",
    )
    ap.add_argument(
        "--feather-m",
        type=float,
        default=bpc.FEATHER_M,
        help="Blend prison/outdoor across paste edge over this many metres (0=hard cut)",
    )
    ap.add_argument(
        "--canvas-inset-m",
        type=float,
        default=bpc.CANVAS_INSET_M,
        help="Pull paste inward from prison DEM/ortho tile edge (avoids hard canvas seam)",
    )
    ap.add_argument(
        "--rock-textures",
        type=Path,
        default=mm.DEFAULT_ROCK_TEXTURE_DIR,
        help="TLD rock albedo folder (same as make_map_bg)",
    )
    ap.add_argument(
        "--no-rock-textures",
        action="store_true",
        help="Flat rock fill instead of tiled albedos",
    )
    args = ap.parse_args()
    args.dump_root = args.dump_root or require_dump_root()

    outdoor = args.dump_root / bpc.OUTDOOR_SCENE
    out = args.out or (
        ROOT / "out" / "maps" / "map_bg_BlackrockRegion_with_prison_yardmask_raw_color.png"
    )

    samples = load_json(outdoor / "alignment_samples.json")
    world, mapxy = collect_samples(samples, prefer_tile="terrain_01")
    A = fit_affine(world, mapxy)
    extent = resolve_map_extent(outdoor, mapxy)
    width, height = output_size_for_extent(args.size, extent, 0)
    print(f"canvas {width}x{height} (BlackrockRegion UV)")

    tile = main_tile_stem(outdoor)
    meters, meta = load_tile_heights(outdoor, tile)
    dem, valid, edge_dist = warp_dem_to_image(
        meters, meta, A, extent, width, height, return_edge_distance=True
    )
    display_valid = valid.copy()
    for stem in list_terrain_stems(outdoor):
        if stem == tile:
            continue
        try:
            tmeters, tmeta = load_tile_heights(outdoor, stem)
            tdem, tvalid, tedge = warp_dem_to_image(
                tmeters, tmeta, A, extent, width, height, return_edge_distance=True
            )
        except Exception as ex:
            print(f"skip outdoor {stem}: {ex}")
            continue
        if mm._is_backdrop_tile(tmeta) or float(tmeta.get("heightNormalizedMax", 0.0)) == 0.0:
            display_valid = display_valid | (tvalid & np.isfinite(tdem))
            continue
        both = valid & tvalid & np.isfinite(dem) & np.isfinite(tdem)
        only_t = tvalid & np.isfinite(tdem) & ~valid
        prefer_t = both & (tedge > edge_dist)
        dem = np.where(prefer_t, tdem, dem)
        dem = np.where(only_t, tdem, dem)
        valid = valid | (tvalid & np.isfinite(tdem))
        display_valid = display_valid | (tvalid & np.isfinite(tdem))
        edge_dist = np.where(prefer_t, tedge, edge_dist)
        edge_dist = np.where(only_t, tedge, edge_dist)

    paste = bpc.try_apply_prison_paste(
        outdoor_dump=outdoor,
        repo_root=ROOT,
        A=A,
        extent=extent,
        width=width,
        height=height,
        dem_outdoor=dem,
        valid_outdoor=valid,
        display_valid=display_valid,
        yard_mask=args.yard_mask,
        full_dem_hole=args.full_dem_hole,
        canvas_inset_m=float(args.canvas_inset_m),
        feather_m=float(args.feather_m),
    )
    if paste is None:
        raise SystemExit("prison paste failed (missing dump/mask/enrichment)")

    dem = paste.dem
    valid = paste.valid
    display_valid = paste.display_valid
    enrich_cover = paste.enrich_cover
    enrich_outline = paste.enrich_outline
    enrich_outline_structure = paste.enrich_outline_structure
    enrich_rock_fill = paste.enrich_rock_fill
    enrich_structure_fill = paste.enrich_structure_fill
    enrich_rock_kinds = paste.enrich_rock_kinds
    enrich_rock_snow = paste.enrich_rock_snow
    enrich_rock_kind_labels = paste.enrich_rock_kind_labels

    orgb, ometa = bpc.load_ortho_land_first(outdoor)
    owarp, ovalid = warp_ortho_to_image(orgb, ometa, A, extent, width, height)
    owarp, ovalid = bpc.blend_ortho_with_prison(
        owarp,
        ovalid,
        prison_dump=paste.prison_dump,
        A=A,
        extent=extent,
        width=width,
        height=height,
        alpha=paste.alpha,
    )

    if not args.no_mask:
        try:
            mask_png, sidecar = mm.resolve_mask_paths(outdoor, bpc.OUTDOOR_SCENE, None)
            painted, mask_meta = mm.load_exclusion_mask(mask_png, sidecar)
            warped, wvalid = warp_dem_to_image(painted, mask_meta, A, extent, width, height)
            outside_template = ~wvalid
            excluded = mm.grow_mask_m(wvalid & (warped > 0.5), A, extent, 0.0) | outside_template
            cut = excluded
            inset_m = 20.0
            if excluded.any() and inset_m > 0:
                dz, dx = mm.world_pixel_size(A, extent, excluded.shape)
                dist_in = ndimage.distance_transform_edt(excluded, sampling=(dz, dx))
                cut = excluded & (dist_in >= inset_m)
            valid = valid & ~cut
            display_valid = display_valid & ~cut
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
            ovalid = ovalid & display_valid
            print(f"mask applied: display {display_valid.mean()*100:.1f}%")
        except FileNotFoundError as e:
            print(f"no mask, continuing: {e}")

    contour_gentle, contour_steep = mm.contour_masks(dem, valid, 1.0)
    rock_bank: dict = {}
    if not args.no_rock_textures:
        rock_bank = mm.load_rock_texture_bank(args.rock_textures)
        print(f"rock textures: {len(rock_bank)} from {args.rock_textures}")

    img = mm.render_raw_composite(
        dem,
        valid,
        owarp,
        ovalid,
        display_valid=display_valid,
        contour_m=1.0,
        contour_gentle_mask=contour_gentle,
        contour_steep_mask=contour_steep,
        enrich_cover=enrich_cover,
        enrich_outline=enrich_outline,
        enrich_outline_structure=enrich_outline_structure,
        enrich_rock_fill=enrich_rock_fill,
        enrich_rock_kinds=enrich_rock_kinds,
        enrich_rock_kind_labels=enrich_rock_kind_labels,
        enrich_rock_snow=enrich_rock_snow,
        rock_texture_bank=rock_bank,
        enrich_structure_fill=enrich_structure_fill,
        shade_floor=0.18,
        outside_rgb=mm.SHIP_OUTSIDE_RGB,
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    img.convert("RGB").save(out)
    print("wrote", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
