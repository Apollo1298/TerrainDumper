#!/usr/bin/env python3
"""Preview reachable occupancy graph over mask template.

Usage:
  python tools/preview_reachable.py AirfieldRegion
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

from pathfinding.common import graph_dir, load_config, load_exclusion_mask, load_json  # noqa: E402
from pathfinding.graph import load_graph  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("scene")
    ap.add_argument("--config", type=Path, default=None)
    args = ap.parse_args()

    cfg = load_config(args.config)
    gdir = graph_dir(cfg, args.scene)
    prefix = gdir / "scene_graph"
    if not Path(str(prefix) + ".json").is_file():
        print(f"Missing {prefix}.json — run build_scene_graph.py first", file=sys.stderr)
        return 1

    g = load_graph(prefix)
    masks_root = Path(cfg["masksRoot"])
    tpl_path = masks_root / args.scene / "template.png"
    if not tpl_path.is_file():
        print(f"Missing template {tpl_path}", file=sys.stderr)
        return 1

    tpl = Image.open(tpl_path).convert("RGB")
    tw, th = tpl.size
    excl_pack = load_exclusion_mask(cfg, args.scene)
    excl_meta = load_json(masks_root / args.scene / "mask.json")
    mpp = float(excl_meta["metersPerPixel"])
    ox = float(excl_meta["originX"])
    max_z = float(excl_meta["maxZ"])

    # Paint reachable cells green on template
    arr = np.asarray(tpl).astype(np.float32) * 0.35
    if excl_pack:
        excl, _ = excl_pack
        playable = excl <= 10
    else:
        playable = np.ones((th, tw), dtype=bool)

    # Mark playable dim
    out = arr.copy()
    out[~playable] = arr[~playable] * 0.5 + np.array([100.0, 30.0, 30.0]) * 0.5

    reach = np.zeros((th, tw), dtype=bool)
    for i in range(g["n"]):
        x, z = float(g["positions"][i, 0]), float(g["positions"][i, 1])
        tx = int(np.floor((x - ox) / mpp))
        ty = int(np.floor((max_z - z) / mpp))
        if 0 <= tx < tw and 0 <= ty < th:
            reach[ty, tx] = True

    out[reach & playable] = out[reach & playable] * 0.25 + np.array([40.0, 220.0, 70.0]) * 0.75

    # Seed markers from graph json
    meta = load_json(Path(str(prefix) + ".json"))
    for s in meta.get("seeds") or []:
        if s.get("node") is None:
            continue
        x, z = float(s["x"]), float(s["z"])
        tx = int(np.floor((x - ox) / mpp))
        ty = int(np.floor((max_z - z) / mpp))
        if 0 <= tx < tw and 0 <= ty < th:
            color = {
                "portal_out": np.array([40.0, 140.0, 255.0]),
                "marker": np.array([255.0, 220.0, 40.0]),
                "player": np.array([255.0, 80.0, 255.0]),
            }.get(s.get("kind"), np.array([255.0, 255.0, 255.0]))
            for dy in range(-2, 3):
                for dx in range(-2, 3):
                    yy, xx = ty + dy, tx + dx
                    if 0 <= xx < tw and 0 <= yy < th:
                        out[yy, xx] = color

    im = Image.fromarray(np.clip(out, 0, 255).astype(np.uint8))
    out_path = gdir / "reachable_overlay.png"
    im.save(out_path)
    preview = ROOT / "out" / f"{args.scene.lower()}_reachable_overlay.png"
    im.resize((900, 900), Image.LANCZOS).save(preview, optimize=True)
    print(f"wrote {out_path}")
    print(f"wrote {preview}")
    print(f"reachable_nodes={g['n']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
