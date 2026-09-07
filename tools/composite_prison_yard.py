"""Flood-fill Blackrock prison yard inside outdoor wall ring; composite prison dump.

Uses BlackrockRegion structure enrichment as the barrier, dilates to close gate gaps,
flood-fills from a yard seed, then pastes BlackrockPrisonSurvivalZone ortho + structure
rim into map_bg_BlackrockRegion over that mask.

Preview only — does not overwrite the ship map unless --out points there.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image
from scipy import ndimage

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from mapalign import (  # noqa: E402
    apply_affine,
    collect_samples,
    enrichment_structure_ids,
    fit_affine,
    invert_affine,
    load_enrichment,
    load_json,
    map_xy_grid,
    output_size_for_extent,
    resolve_map_extent,
    warp_label_to_image,
    warp_ortho_to_image,
    world_to_pixel,
)


def load_ortho(dump: Path) -> tuple[np.ndarray, dict]:
    # Blackrock outdoor + prison: land-first (same as make_map_bg / template).
    for stem in ("ortho_color_tiled_land", "ortho_color_tiled"):
        png = dump / f"{stem}.png"
        meta_path = dump / f"{stem}_meta.json"
        if png.is_file() and meta_path.is_file():
            meta = load_json(meta_path)
            rgb = np.asarray(Image.open(png).convert("RGB"), dtype=np.uint8)
            return rgb, meta
    raise FileNotFoundError(f"no ortho_color_tiled_land/tiled in {dump}")


def yard_flood_mask(
    structure: np.ndarray,
    seed_yx: tuple[int, int],
    *,
    dilate_px: int,
    bbox: tuple[int, int, int, int] | None = None,
) -> tuple[np.ndarray, dict]:
    """Seal wall gaps by dilating structure, fill holes, keep seed cavity.

    Thin outdoor wall traces leave large gate gaps; a modest dilate (~16 px at
    4096) closes them so binary_fill_holes captures the yard. We then grow the
    cavity back toward the walls and clear original structure pixels so the
    outdoor wall rim stays.
    """
    if bbox is None:
        y0b, y1b, x0b, x1b = 0, structure.shape[0], 0, structure.shape[1]
    else:
        y0b, y1b, x0b, x1b = bbox

    crop = structure[y0b:y1b, x0b:x1b]
    dilate_i = max(1, int(dilate_px))
    sealed = ndimage.binary_dilation(crop, iterations=dilate_i)
    filled = ndimage.binary_fill_holes(sealed)
    hole = filled & ~sealed
    if not hole.any():
        raise RuntimeError(
            f"no holes after dilate={dilate_i} — perimeter still open; try higher --dilate-px"
        )

    labeled, nlab = ndimage.label(hole)
    sy, sx = seed_yx
    ly, lx = sy - y0b, sx - x0b
    if not (0 <= ly < hole.shape[0] and 0 <= lx < hole.shape[1]):
        raise ValueError(f"seed ({sy},{sx}) outside bbox")

    # Seed may sit in the dilated wall band; prefer filled, then nearest hole px
    lab = 0
    if hole[ly, lx]:
        lab = int(labeled[ly, lx])
    elif filled[ly, lx]:
        yy, xx = np.ogrid[: hole.shape[0], : hole.shape[1]]
        dist = (yy - ly) ** 2 + (xx - lx) ** 2
        dist[~hole] = np.iinfo(np.int32).max
        ly2, lx2 = np.unravel_index(int(np.argmin(dist)), hole.shape)
        lab = int(labeled[ly2, lx2])
        ly, lx = int(ly2), int(lx2)
    else:
        counts = np.bincount(labeled.ravel())
        counts[0] = 0
        lab = int(np.argmax(counts)) if counts.max() > 0 else 0

    if lab == 0:
        raise RuntimeError("could not resolve yard cavity component")

    cavity = labeled == lab
    # Grow back through ~half the seal so paste reaches the inner wall face
    grow = max(1, dilate_i // 2)
    cavity = ndimage.binary_dilation(cavity, iterations=grow)
    # Stay inside the filled footprint; don't paint over outdoor wall pixels
    cavity &= filled
    cavity &= ~crop

    yard = np.zeros_like(structure, dtype=bool)
    yard[y0b:y1b, x0b:x1b] = cavity

    touches = bool(
        cavity[0, :].any()
        or cavity[-1, :].any()
        or cavity[:, 0].any()
        or cavity[:, -1].any()
    )
    stats = {
        "dilate_px": dilate_i,
        "grow_px": grow,
        "component_label": lab,
        "n_components": int(nlab),
        "yard_px": int(yard.sum()),
        "hole_px": int(np.count_nonzero(labeled == lab)),
        "filled_px": int(np.count_nonzero(filled)),
        "sealed_px": int(np.count_nonzero(sealed)),
        "touches_bbox_border": touches,
        "seed_used": (int(ly + y0b), int(lx + x0b)),
    }
    return yard, stats


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--dump-root",
        type=Path,
        default=Path(r"I:\SteamLibrary\steamapps\common\TheLongDark\Mods\TerrainDumper"),
    )
    ap.add_argument("--size", type=int, default=4096)
    ap.add_argument("--dilate-px", type=int, default=16, help="Wall dilate to seal gaps before hole-fill")
    ap.add_argument(
        "--seed-xz",
        type=float,
        nargs=2,
        default=(-196.0, 63.0),
        help="World XZ seed inside yard",
    )
    ap.add_argument(
        "--out-dir",
        type=Path,
        default=ROOT / "out" / "maps",
    )
    args = ap.parse_args()

    outdoor = args.dump_root / "BlackrockRegion"
    prison = args.dump_root / "BlackrockPrisonSurvivalZone"
    base_map = args.out_dir / "map_bg_BlackrockRegion_new.png"
    if not base_map.is_file():
        raise SystemExit(f"missing base map {base_map}")

    samples = load_json(outdoor / "alignment_samples.json")
    world, mapxy = collect_samples(samples, prefer_tile="terrain_01")
    A = fit_affine(world, mapxy)
    extent = resolve_map_extent(outdoor, mapxy)
    width, height = output_size_for_extent(args.size, extent, 0)
    print(f"map {width}x{height} extent={extent}")

    loaded = load_enrichment(outdoor)
    if loaded is None:
        raise SystemExit("outdoor enrichment missing")
    _, _, emeta = loaded
    eclass, cvalid = warp_label_to_image(emeta["classes"], emeta, A, extent, width, height)
    labels = emeta.get("classLabels") or []
    struct_ids = enrichment_structure_ids(labels)
    structure = cvalid & np.isin(eclass, list(struct_ids))

    # BBox around prison compound (world) with pad
    xmin, xmax, zmin, zmax = -360.0, 40.0, -80.0, 230.0
    mx, my = map_xy_grid(width, height, extent)
    B = invert_affine(A)
    wld = apply_affine(B, np.column_stack([mx.ravel(), my.ravel()]))
    wx = wld[:, 0].reshape(height, width)
    wz = wld[:, 1].reshape(height, width)
    near = (wx >= xmin) & (wx <= xmax) & (wz >= zmin) & (wz <= zmax)
    ys, xs = np.where(near)
    y0b, y1b = int(ys.min()), int(ys.max()) + 1
    x0b, x1b = int(xs.min()), int(xs.max()) + 1
    print(f"prison bbox px: x={x0b}..{x1b} y={y0b}..{y1b}")

    seed_xz = np.array([[args.seed_xz[0], args.seed_xz[1]]], dtype=np.float64)
    seed_px = world_to_pixel(seed_xz, A, extent, width, height)[0]
    seed_yx = (int(round(seed_px[1])), int(round(seed_px[0])))
    print(f"seed world=({args.seed_xz[0]}, {args.seed_xz[1]}) px=({seed_yx[1]},{seed_yx[0]})")

    yard, stats = yard_flood_mask(
        structure,
        seed_yx,
        dilate_px=args.dilate_px,
        bbox=(y0b, y1b, x0b, x1b),
    )
    print("flood stats:", json.dumps(stats))
    if stats["touches_bbox_border"]:
        print(
            "WARNING: yard component touches bbox border — likely leaked through a wall gap. "
            "Try higher --dilate-px."
        )

    args.out_dir.mkdir(parents=True, exist_ok=True)

    # Debug overlay: walls=red, yard=cyan, seed=yellow
    dbg = np.zeros((height, width, 3), dtype=np.uint8)
    dbg[structure] = (200, 40, 40)
    dbg[yard] = (40, 200, 220)
    sy, sx = stats["seed_used"]
    dbg[max(0, sy - 3) : sy + 4, max(0, sx - 3) : sx + 4] = (255, 255, 40)
    crop = dbg[y0b:y1b, x0b:x1b]
    mask_path = args.out_dir / "debug_blackrock_yard_flood.png"
    Image.fromarray(crop).save(mask_path)
    print("wrote", mask_path)

    # Full-size yard mask PNG (white=insert)
    mask_full = args.out_dir / "debug_blackrock_yard_mask.png"
    Image.fromarray((yard.astype(np.uint8) * 255)).save(mask_full)
    print("wrote", mask_full)

    # Composite: prison ortho into yard on existing map
    base = np.asarray(Image.open(base_map).convert("RGB"), dtype=np.float32)
    if base.shape[0] != height or base.shape[1] != width:
        print(f"resizing base map {base.shape[1]}x{base.shape[0]} -> {width}x{height}")
        base = np.asarray(
            Image.fromarray(base.astype(np.uint8)).resize((width, height), Image.Resampling.LANCZOS),
            dtype=np.float32,
        )

    orgb, ometa = load_ortho(prison)
    # Prison dump may have its own alignment — prefer outdoor A (shared map_bg / fog)
    owarp, ovalid = warp_ortho_to_image(orgb, ometa, A, extent, width, height)
    owarp = owarp.astype(np.float32)

    # Prison structure class for enrichment tint on top of ortho paste
    pload = load_enrichment(prison)
    struct_fill = np.zeros((height, width), dtype=bool)
    if pload is not None:
        _, _, pmeta = pload
        if pmeta.get("classes") is not None:
            pclass, pvalid = warp_label_to_image(
                pmeta["classes"], pmeta, A, extent, width, height
            )
            pids = enrichment_structure_ids(pmeta.get("classLabels"))
            struct_fill = pvalid & np.isin(pclass, list(pids))

    paste = yard & ovalid
    out = base.copy()
    for c in range(3):
        out[..., c] = np.where(paste, owarp[..., c], out[..., c])

    # Darken prison structure cells slightly so buildings read on charcoal
    struct_on = yard & struct_fill
    if struct_on.any():
        ink = np.array([28.0, 26.0, 24.0])
        a = 0.35
        for c in range(3):
            out[..., c] = np.where(struct_on, out[..., c] * (1 - a) + ink[c] * a, out[..., c])
        print(f"prison structure tint: {int(struct_on.sum())} px")

    print(f"pasted ortho: {int(paste.sum())} px (yard={int(yard.sum())} ovalid_in_yard={int((yard & ovalid).sum())})")

    comp_path = args.out_dir / "map_bg_BlackrockRegion_prison_composite.png"
    Image.fromarray(np.clip(out, 0, 255).astype(np.uint8)).save(comp_path)
    print("wrote", comp_path)

    # Crop preview
    Image.fromarray(np.clip(out[y0b:y1b, x0b:x1b], 0, 255).astype(np.uint8)).save(
        args.out_dir / "debug_blackrock_prison_composite_crop.png"
    )
    print("wrote", args.out_dir / "debug_blackrock_prison_composite_crop.png")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
