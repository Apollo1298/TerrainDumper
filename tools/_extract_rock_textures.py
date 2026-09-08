#!/usr/bin/env python3
"""Extract TLD rock albedos for map rock-fill textures.

Requires TLD_PATH (or --game-path) and UnityPy.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import UnityPy

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tools"))
from tld_paths import require_tld_path  # noqa: E402

OUT = REPO / "out" / "textures" / "tld_rock_samples"

WANT = {
    "TRN_Rock04",
    "TRN_Rock07_Win",
    "TRN_Rock07_Spg",
    "TRN_Rock07_Flat_Win",
    "TRN_Rock08_C",
    "TRN_Rock08_D",
    "TRN_Rock08_E",
    "TRN_Rock09_A",
    "TRN_Rock09_A_Flat",
    "TRN_Rock09_B",
    "TRN_RockCliff_08_A_Tiled",
    "TRN_Rock_A_Noise",
    "TRN_RockMid01_Win",
    "OBJ_CaveRock_V9_D",
    "GLB_BrickRocks_A",
}


def tex_name(data) -> str:
    return getattr(data, "m_Name", None) or getattr(data, "name", None) or ""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--game-path",
        type=Path,
        default=None,
        help="TLD install root (default: $TLD_PATH)",
    )
    args = ap.parse_args()
    game = args.game_path or require_tld_path()
    bundle_root = game / "tld_Data" / "StreamingAssets" / "aa" / "StandaloneWindows64"

    OUT.mkdir(parents=True, exist_ok=True)
    # Prefer the known global-texture bundle first, then scan the rest.
    preferred = bundle_root / "06fe64a087041e406a76f4539374d751.bundle"
    bundles = ([preferred] if preferred.exists() else []) + [
        p for p in sorted(bundle_root.glob("*.bundle")) if p != preferred
    ]
    found: set[str] = set()
    for i, path in enumerate(bundles):
        if len(found) >= len(WANT):
            break
        if i and i % 80 == 0:
            print(f"  {i}/{len(bundles)} found={len(found)}")
        try:
            env = UnityPy.load(str(path))
        except Exception:
            continue
        for obj in env.objects:
            if obj.type.name not in ("Texture2D", "Sprite"):
                continue
            try:
                data = obj.read()
            except Exception:
                continue
            name = tex_name(data)
            if name not in WANT or name in found:
                continue
            try:
                img = data.image
            except Exception as e:
                print(f"  skip {name}: {e}")
                continue
            if img is None:
                continue
            img.save(OUT / f"{name}.png")
            found.add(name)
            print(f"  {name} {img.size[0]}x{img.size[1]} <- {path.name}")
    missing = sorted(WANT - found)
    print(f"done {len(found)} missing={missing} -> {OUT}")
    return 0 if not missing else 1


if __name__ == "__main__":
    raise SystemExit(main())
