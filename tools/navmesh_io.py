#!/usr/bin/env python3
"""Load TerrainDumper navmesh raw files (library + tiny CLI sanity check).

Usage:
  python tools/navmesh_io.py path/to/AirfieldRegion
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

from pathfinding.common import load_navmesh, load_portals_doc, load_walkable_mask  # noqa: E402


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    dump = Path(sys.argv[1])
    verts, indices, meta = load_navmesh(dump)
    print(f"scene={meta.get('sceneName')} verts={len(verts)} tris={len(indices)//3}")
    print(f"bounds meta cell={meta.get('cellSize')} rasterized={meta.get('rasterized')}")
    try:
        mask, m2 = load_walkable_mask(dump)
        print(f"mask {mask.shape} covered={int(mask.sum())}/{mask.size}")
    except FileNotFoundError as e:
        print(e)
    if (dump / "portals.json").is_file():
        doc = load_portals_doc(dump)
        print(f"portals={doc.get('portalCount')} markers={doc.get('exitPointMarkerCount')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
