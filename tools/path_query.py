#!/usr/bin/env python3
"""A* path query on stitched (or single-scene) graphs.

Same region:
  python tools/path_query.py --from-scene AirfieldRegion --from-xyz 521 275 -1260 \\
      --to-scene AirfieldRegion --to-xyz 1427 235 155

Cross-region (needs stitched links + dest graph):
  python tools/path_query.py --from-scene AirfieldRegion --from-xyz ... --to-scene HubRegion --to-xyz ...
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

from pathfinding.common import dump_dir, load_config, load_json  # noqa: E402
from pathfinding.graph import (  # noqa: E402
    astar,
    astar_world,
    build_world,
    load_graph,
    nearest_node,
    node_id,
    snap_world,
)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--from-scene", required=True)
    ap.add_argument("--to-scene", required=True)
    ap.add_argument("--from-xyz", nargs=3, type=float, required=True, metavar=("X", "Y", "Z"))
    ap.add_argument("--to-xyz", nargs=3, type=float, required=True, metavar=("X", "Y", "Z"))
    ap.add_argument("--config", type=Path, default=None)
    ap.add_argument("--dump-root", type=Path, default=None)
    ap.add_argument("--walk-speed", type=float, default=None)
    args = ap.parse_args()

    cfg = load_config(args.config)
    if args.dump_root:
        cfg["dumpRoot"] = str(args.dump_root)
    speed = float(args.walk_speed if args.walk_speed is not None else cfg.get("walkSpeedMps", 3.5))

    out_root = Path(cfg["outRoot"])
    scenes_needed = {args.from_scene, args.to_scene}

    # Load any graphs that exist under out/graphs for stitching context
    available = sorted(p.name for p in out_root.iterdir() if (p / "scene_graph.json").is_file()) if out_root.is_dir() else []
    load_scenes = set(available) | scenes_needed

    graphs = {}
    dumps = {}
    for s in sorted(load_scenes):
        prefix = out_root / s / "scene_graph"
        if not Path(str(prefix) + ".json").is_file():
            if s in scenes_needed:
                print(f"Missing scene graph: {prefix}.json — run build_scene_graph.py {s}", file=sys.stderr)
                return 1
            continue
        graphs[s] = load_graph(prefix)
        d = dump_dir(cfg, s)
        if (d / "portals.json").is_file():
            dumps[s] = d

    fx, fy, fz = args.from_xyz
    tx, ty, tz = args.to_xyz

    if args.from_scene == args.to_scene:
        g = graphs[args.from_scene]
        si = nearest_node(g, fx, fz)
        gi = nearest_node(g, tx, tz)
        if si is None or gi is None:
            print("Could not snap endpoints to graph", file=sys.stderr)
            return 1
        result = astar(g, si, gi)
        if result is None:
            print("No path")
            return 1
        dist, path = result
        print(f"distance_m={dist:.1f}")
        print(f"time_s={dist / speed:.1f} (walk_speed={speed} m/s)")
        print(f"nodes={len(path)}")
        print(f"start_node={node_id(args.from_scene, int(g['ix'][si]), int(g['iz'][si]))}")
        print(f"goal_node={node_id(args.to_scene, int(g['ix'][gi]), int(g['iz'][gi]))}")
        return 0

    world = build_world(graphs, dumps, cfg)
    start = snap_world(world, args.from_scene, fx, fz)
    goal = snap_world(world, args.to_scene, tx, tz)
    if start is None or goal is None:
        print("Could not snap endpoints to world graph", file=sys.stderr)
        return 1
    result = astar_world(world, start, goal)
    if result is None:
        print("No path (dest graph missing or portals unresolved?)")
        meta_path = out_root / "world_graph_meta.json"
        if meta_path.is_file():
            meta = load_json(meta_path)
            print(f"unresolved portals: {meta.get('unresolvedCount')}")
        return 1
    dist, path = result
    scenes = []
    for nid in path:
        sc = world["nodes"][nid]["scene"]
        if not scenes or scenes[-1] != sc:
            scenes.append(sc)
    print(f"distance_m={dist:.1f}")
    print(f"time_s={dist / speed:.1f} (walk_speed={speed} m/s)")
    print(f"nodes={len(path)}")
    print(f"scenes={' -> '.join(scenes)}")
    print(f"start={start}")
    print(f"goal={goal}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
