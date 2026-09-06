#!/usr/bin/env python3
"""Overlay the main terrain tile's hillshade onto a charcoal map_bg using alignment_samples.json.

Warps the full height field into map/image space (not sparse dots).

Usage:
  python check_alignment.py <dump_dir> <map_bg.png> [out.png]

Example:
  python check_alignment.py ^
    "I:/SteamLibrary/steamapps/common/TheLongDark/Mods/TerrainDumper/LakeRegion" ^
    "F:/Github/DetailedMaps/Maps/map_bg_LakeRegion_new.png" ^
    alignment_check.png
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mapalign import (  # noqa: E402
    apply_affine,
    collect_samples,
    fit_affine,
    invert_affine,
    load_json,
    main_tile_stem,
    map_xy_grid,
    resolve_map_extent,
)


def hillshade(arr: np.ndarray) -> np.ndarray:
    a = arr.astype(np.float64)
    if a.max() <= a.min():
        return np.zeros_like(a)
    a = (a - a.min()) / (a.max() - a.min())
    dy, dx = np.gradient(a)
    slope = np.pi / 4
    aspect = np.arctan2(-dx, dy)
    steep = np.arctan(np.hypot(dx, dy))
    hill = np.cos(slope) * np.cos(steep) + np.sin(slope) * np.sin(steep) * np.cos(
        aspect - np.pi * 1.25
    )
    hill = (hill - hill.min()) / max(1e-9, hill.max() - hill.min())
    return np.clip(0.35 * a + 0.65 * hill, 0, 1)


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2

    dump = Path(sys.argv[1])
    map_bg_path = Path(sys.argv[2])
    out = Path(sys.argv[3]) if len(sys.argv) > 3 else dump / "alignment_check.png"

    tile = main_tile_stem(dump)
    samples = load_json(dump / "alignment_samples.json")
    fog = load_json(dump / "fog_of_war.json") if (dump / "fog_of_war.json").exists() else {}
    W, M = collect_samples(samples, prefer_tile=tile)
    if len(W) < 3:
        print("Need >=3 valid world→map samples. Open map once, re-dump.")
        return 1

    A = fit_affine(W, M)  # world XZ -> map XY
    pred = apply_affine(A, W)
    err = np.linalg.norm(pred - M, axis=1)
    print(f"affine fit RMSE (map units): {err.mean():.4f}  max={err.max():.4f}  n={len(err)}")
    if fog:
        print("fog bg:", fog.get("mapBackgroundFilename"), "scale", fog.get("heightmapScale"))
        print("mapRadiusConstant:", fog.get("mapRadiusConstant"))

    extent = resolve_map_extent(dump, M)
    print(
        f"map UV extent: [{extent[0]:.1f},{extent[1]:.1f}]..[{extent[2]:.1f},{extent[3]:.1f}] "
        f"span={extent[2]-extent[0]:.1f}"
    )

    tmeta = load_json(dump / f"{tile}_meta.json")
    raw = np.fromfile(dump / tmeta["raw"]["fileName"], dtype="<u2")
    tw = int(tmeta["raw"]["width"])
    th = int(tmeta["raw"]["height"])
    heights = raw.reshape((th, tw)).astype(np.float64) / 65535.0
    shade = hillshade(heights)

    pos = tmeta["position"]
    size = tmeta["size"]

    bg = Image.open(map_bg_path).convert("RGBA")
    bw, bh = bg.size

    # Build overlay by sampling shade at each map pixel via inverse affine
    B = invert_affine(A)  # map XY -> world XZ
    mx, my = map_xy_grid(bw, bh, extent)
    flat = np.column_stack([mx.ravel(), my.ravel()])
    world = apply_affine(B, flat)
    wx = world[:, 0].reshape(bh, bw)
    wz = world[:, 1].reshape(bh, bw)

    sx = (wx - pos["x"]) / size["x"] * (tw - 1)
    sy = (wz - pos["z"]) / size["z"] * (th - 1)
    inside = (sx >= 0) & (sx <= tw - 1) & (sy >= 0) & (sy <= th - 1)

    # bilinear sample shade
    sx0 = np.floor(sx).astype(np.int32)
    sy0 = np.floor(sy).astype(np.int32)
    sx1 = np.clip(sx0 + 1, 0, tw - 1)
    sy1 = np.clip(sy0 + 1, 0, th - 1)
    sx0 = np.clip(sx0, 0, tw - 1)
    sy0 = np.clip(sy0, 0, th - 1)
    fx = sx - sx0
    fy = sy - sy0
    s00 = shade[sy0, sx0]
    s10 = shade[sy0, sx1]
    s01 = shade[sy1, sx0]
    s11 = shade[sy1, sx1]
    sampled = (1 - fx) * (1 - fy) * s00 + fx * (1 - fy) * s10 + (1 - fx) * fy * s01 + fx * fy * s11

    overlay = np.zeros((bh, bw, 4), dtype=np.uint8)
    g = (sampled * 255).astype(np.uint8)
    # Cyan-tinted so it reads against charcoal gray
    overlay[..., 0] = (g * 0.35).astype(np.uint8)
    overlay[..., 1] = (g * 0.85).astype(np.uint8)
    overlay[..., 2] = np.clip(g.astype(np.int16) + 40, 0, 255).astype(np.uint8)
    overlay[..., 3] = np.where(inside, 150, 0).astype(np.uint8)

    # Also draw side-by-side: bg | overlay-only | composite
    ov_img = Image.fromarray(overlay, "RGBA")
    comp = Image.alpha_composite(bg, ov_img)

    # Triple panel for clarity
    panel_w, panel_h = bw, bh
    triple = Image.new("RGB", (panel_w * 3 + 16, panel_h + 8), (20, 20, 20))
    triple.paste(bg.convert("RGB"), (0, 4))
    # solid hillshade in map frame for middle
    mid = np.zeros((bh, bw, 3), dtype=np.uint8)
    mid[..., 0] = np.where(inside, overlay[..., 0], 30)
    mid[..., 1] = np.where(inside, overlay[..., 1], 30)
    mid[..., 2] = np.where(inside, overlay[..., 2], 30)
    triple.paste(Image.fromarray(mid, "RGB"), (panel_w + 8, 4))
    triple.paste(comp.convert("RGB"), (panel_w * 2 + 16, 4))
    triple.save(out)

    # Also write just the composite next to it
    comp_path = out.with_name(out.stem + "_composite.png")
    comp.save(comp_path)
    print(f"wrote {out}")
    print(f"  left = map_bg only | middle = warped height | right = overlay on map")
    print(f"wrote {comp_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
