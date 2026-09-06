#!/usr/bin/env python3
"""Stitch pruned scene graphs using portals.json links.

Usage:
  python tools/stitch_world_graph.py AirfieldRegion HubRegion
  python tools/stitch_world_graph.py --all
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

from pathfinding.common import dump_dir, load_config, write_json  # noqa: E402
from pathfinding.graph import build_world, load_graph  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("scenes", nargs="*", help="Scene names to include")
    ap.add_argument("--all", action="store_true", help="All scenes under out/graphs with scene_graph.json")
    ap.add_argument("--config", type=Path, default=None)
    ap.add_argument("--dump-root", type=Path, default=None)
    args = ap.parse_args()

    cfg = load_config(args.config)
    if args.dump_root:
        cfg["dumpRoot"] = str(args.dump_root)

    out_root = Path(cfg["outRoot"])
    scenes = list(args.scenes)
    if args.all:
        scenes = sorted(p.name for p in out_root.iterdir() if (p / "scene_graph.json").is_file())
    if not scenes:
        print("No scenes specified (pass names or --all)", file=sys.stderr)
        return 2

    graphs = {}
    dumps = {}
    for s in scenes:
        prefix = out_root / s / "scene_graph"
        if not Path(str(prefix) + ".json").is_file():
            print(f"skip missing graph: {s}", file=sys.stderr)
            continue
        graphs[s] = load_graph(prefix)
        d = dump_dir(cfg, s)
        if not (d / "portals.json").is_file():
            print(f"skip missing portals: {s}", file=sys.stderr)
            continue
        dumps[s] = d
        print(f"loaded {s} nodes={graphs[s]['n']}")

    if len(graphs) < 1:
        print("Nothing to stitch", file=sys.stderr)
        return 1

    world = build_world(graphs, dumps, cfg)
    # Persist compact: meta + links; adj as separate npz of string... use JSON for v1 if small
    # Airfield alone ~ tens of k nodes — JSON adj may be large. Save links + per-scene refs.
    meta = {
        "scenes": sorted(graphs.keys()),
        "nodeCount": len(world["nodes"]),
        "linkCount": len(world["links"]),
        "unresolvedCount": len(world["unresolved"]),
        "links": world["links"],
        "unresolved": world["unresolved"],
        "walkSpeedMps": cfg.get("walkSpeedMps", 3.5),
        "portalCostMeters": cfg.get("portalCostMeters", 1.0),
        "note": "Full adj kept in memory via tools/path_query.py loading scene graphs + links",
    }
    out = out_root / "world_graph_meta.json"
    write_json(out, meta)
    print(f"wrote {out}")
    print(f"nodes={meta['nodeCount']} portal_links={meta['linkCount']} unresolved={meta['unresolvedCount']}")
    for u in meta["unresolved"][:20]:
        print("  unresolved:", u)
    if len(meta["unresolved"]) > 20:
        print(f"  ... +{len(meta['unresolved']) - 20} more")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
