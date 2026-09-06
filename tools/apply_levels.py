#!/usr/bin/env python3
"""Apply GIMP-style Levels (Value channel) to a map_bg PNG.

Default params match the ship / GIMP "map" preset:
  input low=0, gamma=0.52, high=117; output 0..225

Usage:
  python tools/apply_levels.py in.png [--out out.png]
      [--in-low 0] [--gamma 0.52] [--in-high 117] [--out-high 225]
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
from PIL import Image


def apply_levels_value(
    img: Image.Image,
    *,
    in_low: float = 0.0,
    gamma: float = 0.52,
    in_high: float = 117.0,
    out_low: float = 0.0,
    out_high: float = 225.0,
) -> Image.Image:
    """GIMP Levels on HSV Value (same as RGB max for grayscale/near-gray)."""
    rgba = img.mode == "RGBA"
    arr = np.asarray(img.convert("RGBA" if rgba else "RGB"), dtype=np.float64)
    rgb = arr[..., :3]
    # Value = max(R,G,B); scale channels by V'/V so hue/sat stay put
    v = rgb.max(axis=2)
    denom = max(in_high - in_low, 1e-6)
    t = (v - in_low) / denom
    t = np.clip(t, 0.0, 1.0)
    inv_gamma = 1.0 / max(gamma, 1e-6)
    v_out = (t**inv_gamma) * (out_high - out_low) + out_low
    v_out = np.clip(v_out, 0.0, 255.0)
    scale = np.ones_like(v)
    nonzero = v > 1e-6
    scale[nonzero] = v_out[nonzero] / v[nonzero]
    out_rgb = np.clip(rgb * scale[..., None], 0.0, 255.0)
    if rgba:
        out = np.concatenate([out_rgb, arr[..., 3:4]], axis=2)
        return Image.fromarray(out.astype(np.uint8), "RGBA")
    return Image.fromarray(out_rgb.astype(np.uint8), "RGB")


def main() -> int:
    ap = argparse.ArgumentParser(description="GIMP Levels (Value) for map_bg PNGs")
    ap.add_argument("input", type=Path, help="Input PNG")
    ap.add_argument("--out", type=Path, default=None, help="Output PNG (default: <stem>_levels.png)")
    ap.add_argument("--in-low", type=float, default=0.0)
    ap.add_argument("--gamma", type=float, default=0.52)
    ap.add_argument("--in-high", type=float, default=117.0)
    ap.add_argument("--out-low", type=float, default=0.0)
    ap.add_argument("--out-high", type=float, default=225.0)
    args = ap.parse_args()

    src = args.input
    out = args.out or src.with_name(src.stem + "_levels.png")
    img = Image.open(src)
    result = apply_levels_value(
        img,
        in_low=args.in_low,
        gamma=args.gamma,
        in_high=args.in_high,
        out_low=args.out_low,
        out_high=args.out_high,
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    result.save(out)
    print(
        f"wrote {out} "
        f"(in=[{args.in_low},{args.in_high}] gamma={args.gamma} "
        f"out=[{args.out_low},{args.out_high}])"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
