"""Shared pathfinding helpers for offline navmesh stitcher."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np

REPO = Path(__file__).resolve().parent.parent.parent
DEFAULT_CONFIG = REPO / "tools" / "pathfinding_config.json"


def load_config(path: Path | None = None) -> dict[str, Any]:
    p = path or DEFAULT_CONFIG
    cfg = json.loads(p.read_text(encoding="utf-8"))
    masks = Path(cfg["masksRoot"])
    if not masks.is_absolute():
        cfg["masksRoot"] = str((REPO / masks).resolve())
    out = Path(cfg["outRoot"])
    if not out.is_absolute():
        cfg["outRoot"] = str((REPO / out).resolve())
    return cfg


def dump_dir(cfg: dict[str, Any], scene: str) -> Path:
    return Path(cfg["dumpRoot"]) / scene


def graph_dir(cfg: dict[str, Any], scene: str) -> Path:
    d = Path(cfg["outRoot"]) / scene
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2), encoding="utf-8")


def load_navmesh(dump: Path) -> tuple[np.ndarray, np.ndarray, dict[str, Any]]:
    """Return verts (N,3) float32, indices (T*3,) int32, walkable_meta."""
    meta = load_json(dump / "walkable_meta.json")
    verts_name = meta.get("verticesFile") or "navmesh_vertices.raw"
    indices_name = meta.get("indicesFile") or "navmesh_indices.raw"
    verts = np.fromfile(dump / verts_name, dtype="<f4").reshape(-1, 3)
    indices = np.fromfile(dump / indices_name, dtype="<i4")
    vc = int(meta.get("vertexCount") or len(verts))
    tc = int(meta.get("triangleCount") or (len(indices) // 3))
    if len(verts) != vc:
        raise ValueError(f"{dump}: vertexCount meta={vc} file={len(verts)}")
    if len(indices) != tc * 3:
        raise ValueError(f"{dump}: triangleCount meta={tc} indices={len(indices)}")
    return verts, indices, meta


def load_walkable_mask(dump: Path) -> tuple[np.ndarray, dict[str, Any]]:
    meta = load_json(dump / "walkable_meta.json")
    if not meta.get("rasterized", True) or not meta.get("maskFile"):
        raise FileNotFoundError(f"{dump}: no walkable raster mask (dump with rasterize=1)")
    w = int(meta["width"])
    h = int(meta["height"])
    mask = np.fromfile(dump / meta["maskFile"], dtype=np.uint8).reshape(h, w)
    return mask, meta


def load_portals_doc(dump: Path) -> dict[str, Any]:
    return load_json(dump / "portals.json")


def is_interior_dest(to_scene: str, cfg: dict[str, Any]) -> bool:
    if to_scene.endswith("Region") or "Transition" in to_scene:
        return False
    if to_scene in cfg.get("connectorScenes", []):
        return False
    for hint in cfg.get("interiorNameHints", []):
        if hint.lower() in to_scene.lower():
            return True
    return not to_scene.endswith("Region")


def is_noisy_marker(name: str, cfg: dict[str, Any]) -> bool:
    for s in cfg.get("noisyMarkerSubstrings", []):
        if s in name:
            return True
    return False


def load_exclusion_mask(cfg: dict[str, Any], scene: str) -> tuple[np.ndarray, dict[str, Any]] | None:
    masks_root = Path(cfg["masksRoot"])
    png = masks_root / scene / "mask.png"
    js = masks_root / scene / "mask.json"
    if not png.is_file() or not js.is_file():
        return None
    from PIL import Image

    meta = load_json(js)
    arr = np.array(Image.open(png).convert("L"))
    return arr, meta


def world_to_grid(
    x: float,
    z: float,
    origin_x: float,
    origin_z: float,
    cell: float,
    width: int,
    height: int,
) -> tuple[int, int] | None:
    ix = int(np.floor((x - origin_x) / cell))
    iz = int(np.floor((z - origin_z) / cell))
    if ix < 0 or iz < 0 or ix >= width or iz >= height:
        return None
    return ix, iz


def grid_to_world(
    ix: int, iz: int, origin_x: float, origin_z: float, cell: float
) -> tuple[float, float]:
    return origin_x + (ix + 0.5) * cell, origin_z + (iz + 0.5) * cell


def node_id(scene: str, ix: int, iz: int) -> str:
    return f"{scene}:{ix}:{iz}"


def parse_node_id(nid: str) -> tuple[str, int, int]:
    scene, sx, sz = nid.rsplit(":", 2)
    return scene, int(sx), int(sz)


def exclusion_blocks_world(
    x: float,
    z: float,
    excl: np.ndarray,
    excl_meta: dict[str, Any],
) -> bool:
    """True if painted mask excludes this world XZ (opaque / bright)."""
    mpp = float(excl_meta["metersPerPixel"])
    ox = float(excl_meta["originX"])
    max_z = float(excl_meta["maxZ"])
    th, tw = excl.shape
    tx = int(np.floor((x - ox) / mpp))
    ty = int(np.floor((max_z - z) / mpp))
    if tx < 0 or ty < 0 or tx >= tw or ty >= th:
        return True
    return bool(excl[ty, tx] > 10)
