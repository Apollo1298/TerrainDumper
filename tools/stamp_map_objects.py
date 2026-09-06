#!/usr/bin/env python3
"""Stamp terrain trees + enrichment protrusions onto warped true-color ortho.

Usage:
  python tools/stamp_map_objects.py <dump_dir> [--baseline map.png] [--out out.png]
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
    collect_samples,
    fit_affine,
    load_enrichment,
    load_json,
    load_tile_heights,
    main_tile_stem,
    resolve_map_extent,
    warp_dem_to_image,
    warp_ortho_to_image,
    world_to_pixel,
)


def main() -> int:
    ap = argparse.ArgumentParser(description="Stamp trees/enrichment onto ortho map")
    ap.add_argument("dump_dir", type=Path)
    ap.add_argument("--baseline", type=Path, default=None)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--ortho", type=str, default="ortho_color_tiled")
    ap.add_argument("--tree-radius", type=float, default=1.4, help="Tree stamp radius in pixels")
    ap.add_argument("--rock-min-m", type=float, default=1.5, help="Min enrichment-over-terrain meters to stamp")
    ap.add_argument("--no-trees", action="store_true")
    ap.add_argument("--no-rocks", action="store_true")
    args = ap.parse_args()

    dump: Path = args.dump_dir
    Image.MAX_IMAGE_PIXELS = None

    size = 2048
    if args.baseline and args.baseline.exists():
        size = Image.open(args.baseline).size[0]

    tile = main_tile_stem(dump)
    samples = load_json(dump / "alignment_samples.json")
    W, M = collect_samples(samples, prefer_tile=tile)
    A = fit_affine(W, M)
    extent = resolve_map_extent(dump, M)

    ortho_path = dump / f"{args.ortho}.png"
    ortho_meta = dump / f"{args.ortho}_meta.json"
    if not ortho_path.exists():
        print(f"Missing {ortho_path}")
        return 1
    orgb = np.asarray(Image.open(ortho_path).convert("RGB"), dtype=np.uint8)
    ometa = load_json(ortho_meta)
    owarp, ovalid = warp_ortho_to_image(orgb, ometa, A, extent, size, size)
    base = np.asarray(owarp, dtype=np.float64).copy()
    base[~ovalid] = (36, 36, 40)

    img = Image.fromarray(np.clip(base, 0, 255).astype(np.uint8), "RGB")
    draw = ImageDraw.Draw(img, "RGBA")

    tree_count = 0
    if not args.no_trees:
        trees_path = dump / "tree_instances.json"
        if not trees_path.exists():
            print(f"No {trees_path.name} — run dump_trees in-game first (trees skipped).")
        else:
            doc = json.loads(trees_path.read_text(encoding="utf-8"))
            inst = doc.get("instances") or []
            if inst:
                xz = np.array([[float(t["x"]), float(t["z"])] for t in inst], dtype=np.float64)
                pix = world_to_pixel(xz, A, extent, size, size)
                r = max(0.8, float(args.tree_radius))
                for i, (px, py) in enumerate(pix):
                    if not (0 <= px < size and 0 <= py < size):
                        continue
                    t = inst[i]
                    # Prefer instance tint; fall back to dark pine
                    cr, cg, cb = int(t.get("r", 40)), int(t.get("g", 70)), int(t.get("b", 45))
                    if cr + cg + cb < 30:
                        cr, cg, cb = 42, 68, 48
                    # darken for map readability
                    cr, cg, cb = int(cr * 0.45), int(cg * 0.50), int(cb * 0.40)
                    hs = float(t.get("heightScale", 1.0))
                    rad = r * (0.65 + 0.5 * min(2.0, hs))
                    x0, y0 = px - rad, py - rad
                    x1, y1 = px + rad, py + rad
                    draw.ellipse([x0, y0, x1, y1], fill=(cr, cg, cb, 200))
                    tree_count += 1
            print(f"stamped trees: {tree_count}/{len(inst)}")

    rock_count = 0
    if not args.no_rocks and (dump / "enrichment_meta.json").exists():
        meters, meta = load_tile_heights(dump, tile)
        tdem, tvalid = warp_dem_to_image(meters, meta, A, extent, size, size)
        loaded = load_enrichment(dump)
        if loaded is not None:
            emeters, _emask, emeta = loaded
            edem, evalid = warp_dem_to_image(emeters, emeta, A, extent, size, size)
            both = tvalid & evalid & np.isfinite(tdem) & np.isfinite(edem)
            delta = np.where(both, edem - tdem, 0.0)
            # subsample peaks so we stamp discrete marks, not a wash
            ys, xs = np.where(delta >= float(args.rock_min_m))
            step = max(1, int(round(size / 512)))
            for y, x in zip(ys[::step], xs[::step]):
                d = float(delta[y, x])
                if d < args.rock_min_m:
                    continue
                rad = 0.9 + min(3.5, d * 0.25)
                shade = int(np.clip(28 + d * 4, 28, 70))
                draw.ellipse(
                    [x - rad, y - rad, x + rad, y + rad],
                    fill=(shade, shade - 2, shade - 4, 185),
                )
                rock_count += 1
            print(f"stamped rock marks: {rock_count} (min dH={args.rock_min_m}m, step={step})")

    out = args.out or Path("out/map_bg_LakeRegion_stamped.png")
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out)
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
