#!/usr/bin/env python3
"""Walking estimate via portals + optional exclusion-mask A* (no NavMesh).

Same scene: path on masks/<Scene>/mask.png (stay out of painted exclude),
  else Euclidean XZ crow-flies.
Cross-scene: portal hops to destination exit markers (small fixed cost).

Usage:
  python tools/crow_flies.py --from-scene AirfieldRegion --from-xyz -1271 241 -969 \\
      --to-scene LakeRegion --to-xyz 505 24 652

  python tools/crow_flies.py --list-portals AirfieldRegion
"""

from __future__ import annotations

import argparse
import heapq
import json
import math
import sys
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "tools" / "pathfinding_config.json"

# 8-connected neighbor deltas (dx, dz) in grid cells
_NEIGH8 = (
    (-1, 0, 1.0),
    (1, 0, 1.0),
    (0, -1, 1.0),
    (0, 1, 1.0),
    (-1, -1, math.sqrt(2)),
    (-1, 1, math.sqrt(2)),
    (1, -1, math.sqrt(2)),
    (1, 1, math.sqrt(2)),
)


def load_config(path: Path | None = None) -> dict[str, Any]:
    p = path or DEFAULT_CONFIG
    cfg = json.loads(p.read_text(encoding="utf-8"))
    masks = Path(cfg.get("masksRoot", "masks"))
    if not masks.is_absolute():
        cfg["masksRoot"] = str((ROOT / masks).resolve())
    return cfg


def dump_dir(cfg: dict[str, Any], scene: str) -> Path:
    return Path(cfg["dumpRoot"]) / scene


def load_portals(dump: Path) -> dict[str, Any]:
    return json.loads((dump / "portals.json").read_text(encoding="utf-8-sig"))


def is_interior(to_scene: str, cfg: dict[str, Any]) -> bool:
    if to_scene.endswith("Region") or "Transition" in to_scene:
        return False
    if to_scene in cfg.get("connectorScenes", []):
        return False
    for hint in cfg.get("interiorNameHints", []):
        if hint.lower() in to_scene.lower():
            return True
    return not to_scene.endswith("Region")


def xz_dist(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


class MaskGrid:
    """Coarse walkable grid from painted exclusion mask (opaque = blocked)."""

    def __init__(self, walk: np.ndarray, origin_x: float, origin_z: float, cell: float):
        self.walk = walk  # bool [iz, ix], iz grows with +Z
        self.origin_x = origin_x
        self.origin_z = origin_z
        self.cell = cell
        self.h, self.w = walk.shape
        self._cache: dict[tuple[tuple[int, int], tuple[int, int]], float | None] = {}

    def world_to_cell(self, x: float, z: float) -> tuple[int, int]:
        ix = int(math.floor((x - self.origin_x) / self.cell))
        iz = int(math.floor((z - self.origin_z) / self.cell))
        return ix, iz

    def in_bounds(self, ix: int, iz: int) -> bool:
        return 0 <= ix < self.w and 0 <= iz < self.h

    def snap(self, x: float, z: float) -> tuple[int, int] | None:
        """Nearest walkable cell to world XZ (BFS)."""
        ix, iz = self.world_to_cell(x, z)
        if self.in_bounds(ix, iz) and self.walk[iz, ix]:
            return ix, iz
        # BFS ring
        q: list[tuple[int, int]] = []
        seen: set[tuple[int, int]] = set()
        start = (max(0, min(self.w - 1, ix)), max(0, min(self.h - 1, iz)))
        q.append(start)
        seen.add(start)
        head = 0
        while head < len(q):
            cx, cz = q[head]
            head += 1
            if self.walk[cz, cx]:
                return cx, cz
            for dx, dz, _ in _NEIGH8:
                nx, nz = cx + dx, cz + dz
                if not self.in_bounds(nx, nz) or (nx, nz) in seen:
                    continue
                seen.add((nx, nz))
                q.append((nx, nz))
        return None

    def path_meters(self, a: tuple[float, float], b: tuple[float, float]) -> float | None:
        sa = self.snap(a[0], a[1])
        sb = self.snap(b[0], b[1])
        if sa is None or sb is None:
            return None
        key = (sa, sb) if sa <= sb else (sb, sa)
        if key in self._cache:
            d = self._cache[key]
            return d
        d = self._astar(sa, sb)
        self._cache[key] = d
        return d

    def _astar(self, start: tuple[int, int], goal: tuple[int, int]) -> float | None:
        if start == goal:
            return 0.0
        gx, gz = goal

        def h(ix: int, iz: int) -> float:
            return math.hypot(ix - gx, iz - gz) * self.cell

        open_h: list[tuple[float, int, int]] = []
        heapq.heappush(open_h, (h(*start), start[0], start[1]))
        g_score = {start: 0.0}
        closed: set[tuple[int, int]] = set()

        while open_h:
            _, cx, cz = heapq.heappop(open_h)
            cur = (cx, cz)
            if cur in closed:
                continue
            if cur == goal:
                return g_score[cur]
            closed.add(cur)
            base = g_score[cur]
            for dx, dz, step in _NEIGH8:
                nx, nz = cx + dx, cz + dz
                if not self.in_bounds(nx, nz) or not self.walk[nz, nx]:
                    continue
                # No corner-cutting through blocked diagonals
                if dx != 0 and dz != 0:
                    if not self.walk[cz, nx] or not self.walk[nz, cx]:
                        continue
                nb = (nx, nz)
                if nb in closed:
                    continue
                tentative = base + step * self.cell
                if tentative < g_score.get(nb, float("inf")):
                    g_score[nb] = tentative
                    heapq.heappush(open_h, (tentative + h(nx, nz), nx, nz))
        return None


def load_mask_grid(cfg: dict[str, Any], scene: str) -> MaskGrid | None:
    masks_root = Path(cfg["masksRoot"])
    png = masks_root / scene / "mask.png"
    js = masks_root / scene / "mask.json"
    if not png.is_file() or not js.is_file():
        return None
    meta = json.loads(js.read_text(encoding="utf-8-sig"))
    excl = np.asarray(Image.open(png).convert("L"), dtype=np.uint8)
    # Fine walkable: not painted exclude (matches pathfinding.common threshold)
    fine_walk = excl <= 10

    origin_x = float(meta["originX"])
    origin_z = float(meta["originZ"])
    max_x = float(meta["maxX"])
    max_z = float(meta["maxZ"])
    mpp = float(meta["metersPerPixel"])
    cell = float(cfg.get("occupancyCellMeters", 4.0))
    if cell < mpp:
        cell = mpp

    gw = max(1, int(math.ceil((max_x - origin_x) / cell)))
    gh = max(1, int(math.ceil((max_z - origin_z) / cell)))
    walk = np.zeros((gh, gw), dtype=bool)
    th, tw = fine_walk.shape

    for iz in range(gh):
        wz = origin_z + (iz + 0.5) * cell
        ty = int(math.floor((max_z - wz) / mpp))
        if ty < 0 or ty >= th:
            continue
        for ix in range(gw):
            wx = origin_x + (ix + 0.5) * cell
            tx = int(math.floor((wx - origin_x) / mpp))
            if tx < 0 or tx >= tw:
                continue
            walk[iz, ix] = bool(fine_walk[ty, tx])

    if not walk.any():
        return None
    return MaskGrid(walk, origin_x, origin_z, cell)


def collect_scene_points(
    cfg: dict[str, Any], scenes: set[str]
) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]], list[dict[str, Any]]]:
    portal_cost = float(cfg.get("portalCostMeters", 1.0))
    points_by_scene: dict[str, list[dict[str, Any]]] = {s: [] for s in scenes}
    portal_edges: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []

    docs: dict[str, dict[str, Any]] = {}
    for s in scenes:
        d = dump_dir(cfg, s)
        if (d / "portals.json").is_file():
            docs[s] = load_portals(d)

    def add_point(scene: str, pid: str, x: float, z: float, **meta: Any) -> str:
        points_by_scene.setdefault(scene, []).append({"id": pid, "x": x, "z": z, **meta})
        return pid

    for scene, doc in docs.items():
        for i, p in enumerate(doc.get("portals", [])):
            to_scene = p["toScene"]
            if is_interior(to_scene, cfg):
                continue
            pid = f"{scene}:portal:{i}:{to_scene}"
            add_point(
                scene,
                pid,
                float(p["x"]),
                float(p["z"]),
                kind="portal_out",
                toScene=to_scene,
                exitPointName=p.get("exitPointName"),
                y=float(p["y"]),
            )

            exit_name = p.get("exitPointName") or ""
            if to_scene not in docs:
                unresolved.append(
                    {"reason": "missing_dest_portals", "from": scene, "to": to_scene, "exit": exit_name}
                )
                continue

            dest_doc = docs[to_scene]
            landing = None
            for m in dest_doc.get("exitPointMarkers", []):
                if (m.get("name") or "") == exit_name:
                    landing = m
                    break
            if landing is None:
                unresolved.append(
                    {"reason": "no_exit_marker", "from": scene, "to": to_scene, "exit": exit_name}
                )
                continue

            lid = f"{to_scene}:land:{exit_name}"
            if not any(pt["id"] == lid for pt in points_by_scene.get(to_scene, [])):
                add_point(
                    to_scene,
                    lid,
                    float(landing["x"]),
                    float(landing["z"]),
                    kind="portal_in",
                    exitPointName=exit_name,
                    y=float(landing["y"]),
                )
            portal_edges.append(
                {
                    "from_id": pid,
                    "to_id": lid,
                    "from_scene": scene,
                    "to_scene": to_scene,
                    "exit": exit_name,
                    "cost": portal_cost,
                }
            )

    return points_by_scene, portal_edges, unresolved


def scene_leg_meters(
    grids: dict[str, MaskGrid | None],
    scene: str,
    a: tuple[float, float],
    b: tuple[float, float],
) -> tuple[float, str]:
    """Return (meters, kind) for an in-scene leg."""
    grid = grids.get(scene)
    if grid is None:
        return xz_dist(a, b), "crow"
    d = grid.path_meters(a, b)
    if d is None:
        return xz_dist(a, b), "crow_fb"
    return d, "mask"


def shortest_crow(
    cfg: dict[str, Any],
    from_scene: str,
    from_xyz: tuple[float, float, float],
    to_scene: str,
    to_xyz: tuple[float, float, float],
) -> dict[str, Any]:
    scenes = {from_scene, to_scene}
    root = Path(cfg["dumpRoot"])
    if root.is_dir():
        for p in root.iterdir():
            if (p / "portals.json").is_file():
                scenes.add(p.name)

    points_by_scene, portal_edges, unresolved = collect_scene_points(cfg, scenes)

    start_id = f"{from_scene}:start"
    goal_id = f"{to_scene}:goal"
    points_by_scene.setdefault(from_scene, []).append(
        {"id": start_id, "x": from_xyz[0], "z": from_xyz[2], "kind": "start", "y": from_xyz[1]}
    )
    points_by_scene.setdefault(to_scene, []).append(
        {"id": goal_id, "x": to_xyz[0], "z": to_xyz[2], "kind": "goal", "y": to_xyz[1]}
    )

    # Load mask grids only for scenes that have points
    grids: dict[str, MaskGrid | None] = {}
    mask_scenes = 0
    for scene, pts in points_by_scene.items():
        if not pts:
            continue
        g = load_mask_grid(cfg, scene)
        grids[scene] = g
        if g is not None:
            mask_scenes += 1

    adj: dict[str, list[tuple[str, float, str]]] = {}

    def link(a: str, b: str, w: float, kind: str) -> None:
        adj.setdefault(a, []).append((b, w, kind))
        adj.setdefault(b, []).append((a, w, kind))

    for scene, pts in points_by_scene.items():
        for i, a in enumerate(pts):
            for b in pts[i + 1 :]:
                w, kind = scene_leg_meters(
                    grids, scene, (a["x"], a["z"]), (b["x"], b["z"])
                )
                link(a["id"], b["id"], w, kind)

    for e in portal_edges:
        link(e["from_id"], e["to_id"], float(e["cost"]), "portal")

    if start_id not in adj and from_scene == to_scene:
        w, kind = scene_leg_meters(
            grids, from_scene, (from_xyz[0], from_xyz[2]), (to_xyz[0], to_xyz[2])
        )
        return {
            "distance_m": w,
            "path": [start_id, goal_id],
            "legs": [{"kind": kind, "from": start_id, "to": goal_id, "meters": w}],
            "unresolved": unresolved,
            "mask_scenes": mask_scenes,
        }

    if start_id not in adj or goal_id not in adj:
        return {
            "distance_m": None,
            "error": "start/goal not connected (missing portals.json?)",
            "unresolved": unresolved,
            "mask_scenes": mask_scenes,
        }

    pq: list[tuple[float, str]] = [(0.0, start_id)]
    dist = {start_id: 0.0}
    came: dict[str, tuple[str, float, str]] = {}
    while pq:
        d, u = heapq.heappop(pq)
        if d > dist.get(u, float("inf")):
            continue
        if u == goal_id:
            break
        for v, w, kind in adj.get(u, []):
            nd = d + w
            if nd < dist.get(v, float("inf")):
                dist[v] = nd
                came[v] = (u, w, kind)
                heapq.heappush(pq, (nd, v))

    if goal_id not in dist:
        return {
            "distance_m": None,
            "error": "no path",
            "unresolved": unresolved,
            "mask_scenes": mask_scenes,
        }

    path = [goal_id]
    legs = []
    cur = goal_id
    while cur != start_id:
        prev, w, kind = came[cur]
        legs.append({"kind": kind, "from": prev, "to": cur, "meters": w})
        path.append(prev)
        cur = prev
    path.reverse()
    legs.reverse()

    return {
        "distance_m": dist[goal_id],
        "path": path,
        "legs": legs,
        "unresolved": unresolved,
        "mask_scenes": mask_scenes,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", type=Path, default=None)
    ap.add_argument("--dump-root", type=Path, default=None)
    ap.add_argument("--walk-speed", type=float, default=None, help="m/s for time estimate")
    ap.add_argument("--list-portals", metavar="SCENE", help="Print outdoor portals for a scene")
    ap.add_argument("--from-scene")
    ap.add_argument("--to-scene")
    ap.add_argument("--from-xyz", nargs=3, type=float, metavar=("X", "Y", "Z"))
    ap.add_argument("--to-xyz", nargs=3, type=float, metavar=("X", "Y", "Z"))
    args = ap.parse_args()

    cfg = load_config(args.config)
    if args.dump_root:
        cfg["dumpRoot"] = str(args.dump_root)
    speed = float(args.walk_speed if args.walk_speed is not None else cfg.get("walkSpeedMps", 3.5))

    if args.list_portals:
        doc = load_portals(dump_dir(cfg, args.list_portals))
        for p in doc.get("portals", []):
            if is_interior(p["toScene"], cfg):
                continue
            print(
                f"{p['toScene']:28s} exit={p.get('exitPointName')}  "
                f"xyz=({p['x']:.1f},{p['y']:.1f},{p['z']:.1f})  go={p.get('gameObject')}"
            )
        return 0

    if not all([args.from_scene, args.to_scene, args.from_xyz, args.to_xyz]):
        ap.print_help()
        return 2

    result = shortest_crow(
        cfg,
        args.from_scene,
        (args.from_xyz[0], args.from_xyz[1], args.from_xyz[2]),
        args.to_scene,
        (args.to_xyz[0], args.to_xyz[1], args.to_xyz[2]),
    )

    if result.get("distance_m") is None:
        print(result.get("error") or "no path", file=sys.stderr)
        for u in result.get("unresolved") or []:
            print("  unresolved:", u, file=sys.stderr)
        return 1

    dist = float(result["distance_m"])
    real_s = dist / speed
    # Default sandbox: 1 real minute ≈ 12 game minutes at day-length 1x
    # (1 game day ≈ 2 real hours). Custom day-length Nx slows the clock by N.
    gpm = float(cfg.get("gameMinutesPerRealMinute", 12.0))
    day_mult = max(1e-6, float(cfg.get("dayLengthMultiplier", 1.0)))
    game_min = (real_s / 60.0) * (gpm / day_mult)
    game_h = int(game_min // 60)
    game_m = game_min - game_h * 60

    print(f"distance_m={dist:.1f}")
    print(f"real_s={real_s:.1f} (walk_speed={speed} m/s)")
    print(
        f"game_time={game_h}h {game_m:.0f}m "
        f"({game_min:.1f} min; {gpm:g} game-min/real-min @ dayLength={day_mult:g}x)"
    )
    print(f"mask_scenes={result.get('mask_scenes', 0)}")
    print(f"legs={len(result['legs'])}")
    for leg in result["legs"]:
        print(f"  {leg['kind']:8s} {leg['meters']:8.1f} m  {leg['from']} -> {leg['to']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
