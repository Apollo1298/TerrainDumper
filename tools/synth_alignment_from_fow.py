#!/usr/bin/env python3
"""Synthesize alignment_samples.json from fow.json (Mod / offline FOW path).

Writes the same shape TerrainDumper dumps for tools that still expect
alignment_samples.json, using the TldevMaps world→map contract:

  scale = mapRadiusConstant * √2 / sceneRadius
  center = midpoint(unionBounds)
  map_x = (world_x - cx) * scale
  map_y = (world_z - cz) * scale

Usage:
  python tools/synth_alignment_from_fow.py path/to/Mods/TerrainDumper/ModShatteredMarsh
  python tools/synth_alignment_from_fow.py path/to/ModShatteredMarsh --out alignment_samples.json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mapalign import (  # noqa: E402
    apply_affine,
    find_fow_config_path,
    list_terrain_stems,
    load_json,
    load_tile_heights,
    main_tile_stem,
    scene_name_for_dump,
    synthesize_world_to_map_affine,
)


def tile_corner_and_grid(meta: dict, stem: str, n: int = 5) -> list[dict]:
    """World probes at tile corners + interior grid (same spirit as MapAlignmentDump)."""
    bmin = meta.get("boundsMin")
    bmax = meta.get("boundsMax")
    if isinstance(bmin, dict) and isinstance(bmax, dict):
        x0, x1 = float(bmin["x"]), float(bmax["x"])
        z0, z1 = float(bmin["z"]), float(bmax["z"])
        y = float(bmin.get("y", meta.get("position", {}).get("y", 0.0)))
    else:
        pos, size = meta["position"], meta["size"]
        x0 = float(pos["x"])
        z0 = float(pos["z"])
        x1 = x0 + float(size["x"])
        z1 = z0 + float(size["z"])
        y = float(pos.get("y", 0.0))

    samples: list[dict] = []
    corners = [
        ("minX_minZ", x0, z0),
        ("minX_maxZ", x0, z1),
        ("maxX_minZ", x1, z0),
        ("maxX_maxZ", x1, z1),
    ]
    for label, x, z in corners:
        samples.append({"tile": stem, "label": label, "world": {"x": x, "y": y, "z": z}})

    for iz in range(n):
        for ix in range(n):
            u = ix / max(1, n - 1)
            v = iz / max(1, n - 1)
            x = x0 + u * (x1 - x0)
            z = z0 + v * (z1 - z0)
            samples.append(
                {
                    "tile": stem,
                    "label": f"grid_{ix}_{iz}",
                    "world": {"x": x, "y": y, "z": z},
                }
            )
    return samples


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dump_dir", type=Path, help="TerrainDumper dump folder")
    ap.add_argument("--fow-config", type=Path, default=None, help="Override fow.json path")
    ap.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Output path (default: <dump>/alignment_samples.json)",
    )
    ap.add_argument(
        "--flip-map-axes",
        action="store_true",
        help="Negate both axes (MountainPass-style 180° RegionMap)",
    )
    ap.add_argument(
        "--all-tiles",
        action="store_true",
        help="Sample every terrain tile (default: primary land tile only)",
    )
    ap.add_argument("--dry-run", action="store_true", help="Print summary; do not write")
    args = ap.parse_args()

    dump: Path = args.dump_dir
    fow_path = find_fow_config_path(dump, args.fow_config)
    if fow_path is None:
        print(f"No fow.json for {dump} — run make_fow_config.py first")
        return 1

    fow = load_json(fow_path)
    A = synthesize_world_to_map_affine(fow, flip_axes=bool(args.flip_map_axes))
    scene = str(fow.get("sceneName") or scene_name_for_dump(dump))
    scale = float(A[0, 0])
    cx = -float(A[0, 2]) / scale if abs(scale) > 1e-12 else 0.0
    cz = -float(A[1, 2]) / float(A[1, 1]) if abs(A[1, 1]) > 1e-12 else 0.0

    stems = list_terrain_stems(dump) if args.all_tiles else [main_tile_stem(dump)]
    raw_samples: list[dict] = []
    for stem in stems:
        _, meta = load_tile_heights(dump, stem)
        raw_samples.extend(tile_corner_and_grid(meta, stem))

    W = np.array([[s["world"]["x"], s["world"]["z"]] for s in raw_samples], dtype=np.float64)
    M = apply_affine(A, W)
    out_samples = []
    for s, m in zip(raw_samples, M):
        out_samples.append(
            {
                **s,
                "map": {"x": float(m[0]), "y": float(m[1]), "z": 0.0},
            }
        )

    payload = {
        "formatVersion": 2,
        "sceneName": scene,
        "synthesizedFrom": str(fow_path),
        "synthesizedAtUtc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "mapSpaceNotes": [
            "Synthesized offline from fow.json (not Panel_Map.WorldPositionToMapPosition).",
            f"scale={scale:.8f} center=({cx:.4f},{cz:.4f})"
            + (" flip_axes" if args.flip_map_axes else ""),
            "Re-dump after tldev_map for a golden live sample set.",
        ],
        "samples": out_samples,
    }

    out = args.out or (dump / "alignment_samples.json")
    print(
        f"{scene}: {len(out_samples)} samples scale={scale:.6f} "
        f"center=({cx:.1f},{cz:.1f}) -> {out}"
    )
    if args.dry_run:
        print(json.dumps({k: payload[k] for k in payload if k != "samples"}, indent=2))
        return 0

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
