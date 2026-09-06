#!/usr/bin/env python3
"""Build a per-scene occupancy graph from walkable_mask (+ portals prune).

Usage:
  python tools/build_scene_graph.py AirfieldRegion
  python tools/build_scene_graph.py AirfieldRegion --no-prune
  python tools/build_scene_graph.py AirfieldRegion --dump-root "I:/.../TerrainDumper"
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

from pathfinding.common import dump_dir, graph_dir, load_config, load_walkable_mask  # noqa: E402
from pathfinding.graph import (  # noqa: E402
    apply_exclusion,
    build_occupancy_from_mask,
    collect_seeds,
    prune_reachable,
    save_graph,
)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("scene", help="Scene folder name under dump root")
    ap.add_argument("--config", type=Path, default=None)
    ap.add_argument("--dump-root", type=Path, default=None)
    ap.add_argument("--no-prune", action="store_true", help="Skip reachability flood-fill")
    ap.add_argument("--no-mask", action="store_true", help="Skip painted exclusion mask")
    args = ap.parse_args()

    cfg = load_config(args.config)
    if args.dump_root:
        cfg["dumpRoot"] = str(args.dump_root)

    dump = dump_dir(cfg, args.scene)
    if not dump.is_dir():
        print(f"Dump not found: {dump}", file=sys.stderr)
        return 1

    mask, meta = load_walkable_mask(dump)
    print(f"mask {mask.shape} cell={meta.get('cellSize')} covered={int(mask.sum())}/{mask.size}")

    g = build_occupancy_from_mask(mask, meta, neighbor_mode=int(cfg.get("neighborMode", 8)))
    print(f"raw graph nodes={g['n']}")

    if not args.no_mask:
        g = apply_exclusion(g, cfg)
        print(f"after exclusion nodes={g['n']} applied={g.get('exclusion_applied')}")

    seed_report = None
    if not args.no_prune:
        seeds, seed_report = collect_seeds(g, dump, cfg)
        print(f"seeds snapped={len(seeds)} / report={len(seed_report)}")
        g = prune_reachable(g, seeds)
        print(f"reachable nodes={g['n']}")

    out = graph_dir(cfg, args.scene) / "scene_graph"
    save_graph(out, g, seed_report)
    print(f"wrote {out}.npz / {out}.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
