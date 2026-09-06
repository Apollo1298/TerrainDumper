#!/usr/bin/env python3
"""Offline height preview from a TerrainDumper dump folder.

Usage:
  python preview_heights.py path/to/Mods/TerrainDumper/LakeRegion
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

try:
    import numpy as np
except ImportError:
    print("Requires numpy: pip install numpy")
    sys.exit(1)

try:
    from PIL import Image
except ImportError:
    Image = None  # type: ignore


def load_raw(meta_path: Path) -> tuple[np.ndarray, dict]:
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    raw_info = meta["raw"]
    raw_path = meta_path.parent / raw_info["fileName"]
    w = int(raw_info["width"])
    h = int(raw_info["height"])
    data = np.fromfile(raw_path, dtype="<u2").reshape((h, w))
    return data, meta


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2

    folder = Path(sys.argv[1])
    metas = sorted(folder.glob("terrain_*_meta.json"))
    if not metas:
        print(f"No terrain_*_meta.json in {folder}")
        return 1

    for meta_path in metas:
        arr, meta = load_raw(meta_path)
        stem = meta.get("stem", meta_path.stem.replace("_meta", ""))
        # normalize for display
        f = arr.astype(np.float32) / 65535.0
        out = folder / f"{stem}_preview_offline.png"
        if Image is None:
            # fallback: write simple PGM
            pgm = folder / f"{stem}_preview_offline.pgm"
            u8 = (f * 255).astype(np.uint8)
            with pgm.open("wb") as fh:
                fh.write(f"P5\n{u8.shape[1]} {u8.shape[0]}\n255\n".encode("ascii"))
                fh.write(u8.tobytes())
            print(f"Wrote {pgm} (install Pillow for PNG)")
        else:
            u8 = (f * 255).astype(np.uint8)
            Image.fromarray(u8, mode="L").save(out)
            print(f"Wrote {out}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
