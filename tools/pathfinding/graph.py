"""Occupancy graph build, reachability prune, stitch, A*."""

from __future__ import annotations

import heapq
import math
from collections import defaultdict, deque
from typing import Any

import numpy as np

from .common import (
    exclusion_blocks_world,
    grid_to_world,
    is_interior_dest,
    is_noisy_marker,
    load_exclusion_mask,
    load_portals_doc,
    load_walkable_mask,
    node_id,
    parse_node_id,
    world_to_grid,
    write_json,
)


def build_occupancy_from_mask(
    mask: np.ndarray,
    meta: dict[str, Any],
    *,
    neighbor_mode: int = 8,
) -> dict[str, Any]:
    """Build 4/8-connected graph from walkable_mask.raw grid."""
    h, w = mask.shape
    cell = float(meta["cellSize"])
    ox = float(meta["originX"])
    oz = float(meta["originZ"])
    scene = meta.get("sceneName") or "Unknown"

    walk = mask > 0
    # Map (iz, ix) -> linear node index among walkable cells
    ys, xs = np.nonzero(walk)
    n = len(xs)
    key_to_i = {(int(iz), int(ix)): i for i, (iz, ix) in enumerate(zip(ys, xs))}

    offsets = [(-1, 0), (1, 0), (0, -1), (0, 1)]
    if neighbor_mode == 8:
        offsets += [(-1, -1), (-1, 1), (1, -1), (1, 1)]

    # adjacency as list of (neighbor_index, weight_m)
    adj: list[list[tuple[int, float]]] = [[] for _ in range(n)]
    for i, (iz, ix) in enumerate(zip(ys, xs)):
        iz = int(iz)
        ix = int(ix)
        for dz, dx in offsets:
            j = key_to_i.get((iz + dz, ix + dx))
            if j is None:
                continue
            # Euclidean in XZ between cell centers
            wgt = cell * math.hypot(dx, dz)
            adj[i].append((j, wgt))

    positions = np.column_stack(
        [
            ox + (xs.astype(np.float64) + 0.5) * cell,
            oz + (ys.astype(np.float64) + 0.5) * cell,
        ]
    )

    return {
        "scene": scene,
        "cell": cell,
        "origin_x": ox,
        "origin_z": oz,
        "width": w,
        "height": h,
        "neighbor_mode": neighbor_mode,
        "n": n,
        "ix": xs.astype(np.int32),
        "iz": ys.astype(np.int32),
        "positions": positions,  # (n,2) x,z
        "adj": adj,
        "key_to_i": key_to_i,
    }


def nearest_node(graph: dict[str, Any], x: float, z: float, *, max_dist: float = 50.0) -> int | None:
    pos = graph["positions"]
    if len(pos) == 0:
        return None
    d2 = (pos[:, 0] - x) ** 2 + (pos[:, 1] - z) ** 2
    i = int(np.argmin(d2))
    if d2[i] > max_dist * max_dist:
        return None
    return i


def apply_exclusion(graph: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
    """Drop nodes whose centers fall in painted exclude mask."""
    scene = graph["scene"]
    excl_pack = load_exclusion_mask(cfg, scene)
    if excl_pack is None:
        graph["exclusion_applied"] = False
        return graph
    excl, excl_meta = excl_pack
    keep = []
    for i in range(graph["n"]):
        x, z = graph["positions"][i]
        if exclusion_blocks_world(float(x), float(z), excl, excl_meta):
            continue
        keep.append(i)
    return _subgraph(graph, keep, extra={"exclusion_applied": True})


def _subgraph(graph: dict[str, Any], keep: list[int], extra: dict[str, Any] | None = None) -> dict[str, Any]:
    if len(keep) == graph["n"] and not extra:
        return graph
    old_to_new = {old: new for new, old in enumerate(keep)}
    adj: list[list[tuple[int, float]]] = [[] for _ in keep]
    for new_i, old_i in enumerate(keep):
        for old_j, w in graph["adj"][old_i]:
            new_j = old_to_new.get(old_j)
            if new_j is not None:
                adj[new_i].append((new_j, w))
    key_to_i = {}
    for new_i, old_i in enumerate(keep):
        iz = int(graph["iz"][old_i])
        ix = int(graph["ix"][old_i])
        key_to_i[(iz, ix)] = new_i
    out = {
        "scene": graph["scene"],
        "cell": graph["cell"],
        "origin_x": graph["origin_x"],
        "origin_z": graph["origin_z"],
        "width": graph["width"],
        "height": graph["height"],
        "neighbor_mode": graph["neighbor_mode"],
        "n": len(keep),
        "ix": graph["ix"][keep],
        "iz": graph["iz"][keep],
        "positions": graph["positions"][keep],
        "adj": adj,
        "key_to_i": key_to_i,
    }
    if extra:
        out.update(extra)
    return out


def collect_seeds(graph: dict[str, Any], dump, cfg: dict[str, Any]) -> tuple[list[int], list[dict[str, Any]]]:
    """Seeds from portals (outbound) + useful inbound markers + player."""
    doc = load_portals_doc(dump)
    seeds: list[int] = []
    report: list[dict[str, Any]] = []

    def add_xyz(x: float, y: float, z: float, kind: str, **meta: Any) -> None:
        i = nearest_node(graph, x, z)
        entry = {"kind": kind, "x": x, "y": y, "z": z, "node": i, **meta}
        report.append(entry)
        if i is not None:
            seeds.append(i)

    for p in doc.get("portals", []):
        interior = is_interior_dest(p["toScene"], cfg)
        add_xyz(
            float(p["x"]),
            float(p["y"]),
            float(p["z"]),
            "portal_out",
            toScene=p["toScene"],
            exitPointName=p.get("exitPointName"),
            interior=interior,
            gameObject=p.get("gameObject"),
        )

    for m in doc.get("exitPointMarkers", []):
        name = m.get("name") or ""
        if is_noisy_marker(name, cfg):
            continue
        add_xyz(float(m["x"]), float(m["y"]), float(m["z"]), "marker", name=name)

    if doc.get("playerX") is not None:
        add_xyz(float(doc["playerX"]), float(doc["playerY"]), float(doc["playerZ"]), "player")

    # unique seeds
    seeds = sorted(set(seeds))
    return seeds, report


def prune_reachable(graph: dict[str, Any], seeds: list[int]) -> dict[str, Any]:
    if not seeds:
        out = _subgraph(graph, [], extra={"reachable": True, "seed_count": 0})
        return out
    seen = set()
    q = deque(seeds)
    for s in seeds:
        seen.add(s)
    while q:
        i = q.popleft()
        for j, _ in graph["adj"][i]:
            if j not in seen:
                seen.add(j)
                q.append(j)
    keep = sorted(seen)
    return _subgraph(graph, keep, extra={"reachable": True, "seed_count": len(seeds)})


def save_graph(path_prefix, graph: dict[str, Any], seed_report: list[dict[str, Any]] | None = None) -> None:
    """Save graph as .npz arrays + .json sidecar (no adj in JSON — stored as CSR-like)."""
    from pathlib import Path

    path_prefix = Path(path_prefix)
    path_prefix.parent.mkdir(parents=True, exist_ok=True)

    # Flatten adjacency to CSR
    offsets = [0]
    flat_j: list[int] = []
    flat_w: list[float] = []
    for lst in graph["adj"]:
        for j, w in lst:
            flat_j.append(j)
            flat_w.append(w)
        offsets.append(len(flat_j))

    np.savez_compressed(
        str(path_prefix) + ".npz",
        ix=graph["ix"],
        iz=graph["iz"],
        positions=graph["positions"],
        adj_offsets=np.asarray(offsets, dtype=np.int32),
        adj_j=np.asarray(flat_j, dtype=np.int32),
        adj_w=np.asarray(flat_w, dtype=np.float32),
    )

    meta = {
        "scene": graph["scene"],
        "cell": graph["cell"],
        "originX": graph["origin_x"],
        "originZ": graph["origin_z"],
        "width": graph["width"],
        "height": graph["height"],
        "neighborMode": graph["neighbor_mode"],
        "nodeCount": graph["n"],
        "edgeCount": len(flat_j) // 2,
        "exclusionApplied": bool(graph.get("exclusion_applied")),
        "reachable": bool(graph.get("reachable")),
        "seedCount": graph.get("seed_count"),
        "npz": path_prefix.name + ".npz",
    }
    if seed_report is not None:
        meta["seeds"] = seed_report
    write_json(Path(str(path_prefix) + ".json"), meta)


def load_graph(path_prefix) -> dict[str, Any]:
    from pathlib import Path

    path_prefix = Path(path_prefix)
    meta = __import__("json").loads(Path(str(path_prefix) + ".json").read_text(encoding="utf-8"))
    data = np.load(str(path_prefix) + ".npz")
    offsets = data["adj_offsets"]
    adj_j = data["adj_j"]
    adj_w = data["adj_w"]
    n = int(meta["nodeCount"])
    adj: list[list[tuple[int, float]]] = []
    for i in range(n):
        a, b = int(offsets[i]), int(offsets[i + 1])
        adj.append([(int(adj_j[k]), float(adj_w[k])) for k in range(a, b)])
    ix = data["ix"]
    iz = data["iz"]
    key_to_i = {(int(iz[i]), int(ix[i])): i for i in range(n)}
    return {
        "scene": meta["scene"],
        "cell": float(meta["cell"]),
        "origin_x": float(meta["originX"]),
        "origin_z": float(meta["originZ"]),
        "width": int(meta["width"]),
        "height": int(meta["height"]),
        "neighbor_mode": int(meta["neighborMode"]),
        "n": n,
        "ix": ix,
        "iz": iz,
        "positions": data["positions"],
        "adj": adj,
        "key_to_i": key_to_i,
        "meta": meta,
    }


def astar(
    graph: dict[str, Any],
    start: int,
    goal: int,
) -> tuple[float, list[int]] | None:
    if start == goal:
        return 0.0, [start]
    pos = graph["positions"]
    gx, gz = float(pos[goal, 0]), float(pos[goal, 1])

    def h(i: int) -> float:
        return math.hypot(float(pos[i, 0]) - gx, float(pos[i, 1]) - gz)

    open_h: list[tuple[float, int]] = []
    heapq.heappush(open_h, (h(start), start))
    g_score = {start: 0.0}
    came: dict[int, int] = {}
    closed: set[int] = set()

    while open_h:
        _, current = heapq.heappop(open_h)
        if current in closed:
            continue
        if current == goal:
            path = [current]
            while current in came:
                current = came[current]
                path.append(current)
            path.reverse()
            return g_score[goal], path
        closed.add(current)
        for nb, w in graph["adj"][current]:
            if nb in closed:
                continue
            tentative = g_score[current] + w
            if tentative < g_score.get(nb, float("inf")):
                came[nb] = current
                g_score[nb] = tentative
                heapq.heappush(open_h, (tentative + h(nb), nb))
    return None


def build_world(
    scene_graphs: dict[str, dict[str, Any]],
    dumps: dict[str, Any],
    cfg: dict[str, Any],
) -> dict[str, Any]:
    """
    Stitch scenes. Node IDs are strings scene:ix:iz.
    dumps: scene -> Path dump dir (for portals.json)
    """
    portal_cost = float(cfg.get("portalCostMeters", 1.0))
    # Local node index within scene graph → global string id
    nodes: dict[str, dict[str, Any]] = {}
    adj: dict[str, list[tuple[str, float, str]]] = defaultdict(list)  # (to, w, kind)

    for scene, g in scene_graphs.items():
        for i in range(g["n"]):
            nid = node_id(scene, int(g["ix"][i]), int(g["iz"][i]))
            nodes[nid] = {
                "scene": scene,
                "ix": int(g["ix"][i]),
                "iz": int(g["iz"][i]),
                "x": float(g["positions"][i, 0]),
                "z": float(g["positions"][i, 1]),
            }
            for j, w in g["adj"][i]:
                nid2 = node_id(scene, int(g["ix"][j]), int(g["iz"][j]))
                adj[nid].append((nid2, float(w), "walk"))

    unresolved: list[dict[str, Any]] = []
    links: list[dict[str, Any]] = []

    for scene, dump in dumps.items():
        if scene not in scene_graphs:
            continue
        doc = load_portals_doc(dump)
        g = scene_graphs[scene]
        for p in doc.get("portals", []):
            to_scene = p["toScene"]
            if is_interior_dest(to_scene, cfg):
                continue
            exit_name = p.get("exitPointName") or ""
            i = nearest_node(g, float(p["x"]), float(p["z"]))
            if i is None:
                unresolved.append({"reason": "no_from_node", "from": scene, "portal": p})
                continue
            from_nid = node_id(scene, int(g["ix"][i]), int(g["iz"][i]))

            if to_scene not in scene_graphs:
                unresolved.append({"reason": "missing_dest_graph", "from": scene, "to": to_scene, "exit": exit_name})
                continue

            g2 = scene_graphs[to_scene]
            dest_dump = dumps[to_scene]
            dest_doc = load_portals_doc(dest_dump)
            # Find marker by exact name
            target_xyz = None
            for m in dest_doc.get("exitPointMarkers", []):
                if (m.get("name") or "") == exit_name:
                    target_xyz = (float(m["x"]), float(m["y"]), float(m["z"]))
                    break
            if target_xyz is None:
                # fallback: any outbound portal in dest that points back? or nearest marker fuzzy
                unresolved.append(
                    {"reason": "no_exit_marker", "from": scene, "to": to_scene, "exit": exit_name}
                )
                continue

            j = nearest_node(g2, target_xyz[0], target_xyz[2])
            if j is None:
                unresolved.append(
                    {"reason": "no_to_node", "from": scene, "to": to_scene, "exit": exit_name}
                )
                continue
            to_nid = node_id(to_scene, int(g2["ix"][j]), int(g2["iz"][j]))
            adj[from_nid].append((to_nid, portal_cost, "portal"))
            adj[to_nid].append((from_nid, portal_cost, "portal"))
            links.append(
                {
                    "from": from_nid,
                    "to": to_nid,
                    "fromScene": scene,
                    "toScene": to_scene,
                    "exitPointName": exit_name,
                    "cost": portal_cost,
                }
            )

    return {"nodes": nodes, "adj": dict(adj), "links": links, "unresolved": unresolved}


def astar_world(
    world: dict[str, Any],
    start: str,
    goal: str,
) -> tuple[float, list[str]] | None:
    nodes = world["nodes"]
    adj = world["adj"]
    if start not in nodes or goal not in nodes:
        return None
    if start == goal:
        return 0.0, [start]

    gx, gz = nodes[goal]["x"], nodes[goal]["z"]

    def h(nid: str) -> float:
        n = nodes[nid]
        return math.hypot(n["x"] - gx, n["z"] - gz)

    open_h: list[tuple[float, str]] = []
    heapq.heappush(open_h, (h(start), start))
    g_score = {start: 0.0}
    came: dict[str, str] = {}
    closed: set[str] = set()

    while open_h:
        _, current = heapq.heappop(open_h)
        if current in closed:
            continue
        if current == goal:
            path = [current]
            while current in came:
                current = came[current]
                path.append(current)
            path.reverse()
            return g_score[goal], path
        closed.add(current)
        for nb, w, _kind in adj.get(current, []):
            if nb in closed:
                continue
            tentative = g_score[current] + w
            if tentative < g_score.get(nb, float("inf")):
                came[nb] = current
                g_score[nb] = tentative
                heapq.heappush(open_h, (tentative + h(nb), nb))
    return None


def snap_world(world: dict[str, Any], scene: str, x: float, z: float, max_dist: float = 50.0) -> str | None:
    best = None
    best_d = max_dist * max_dist
    for nid, n in world["nodes"].items():
        if n["scene"] != scene:
            continue
        d = (n["x"] - x) ** 2 + (n["z"] - z) ** 2
        if d < best_d:
            best_d = d
            best = nid
    return best
