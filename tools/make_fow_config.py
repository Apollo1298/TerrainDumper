#!/usr/bin/env python3
"""Derive offline FogOfWar config from a TerrainDumper dump.

Reads terrain_*_meta.json and writes fow.json with sceneRadius = half-diagonal
of the playable-land AABB union (same rule that matches vanilla MountainPassRegion
≈ 2121.3). Does **not** use dump fog_of_war.json (Mod dumps fall back to Ash Canyon).

Usage:
  python tools/make_fow_config.py path/to/Mods/TerrainDumper/ModShatteredMarsh
  python tools/make_fow_config.py path/to/MountainPassRegion --validate
  python tools/make_fow_config.py --all-mod

Writes:
  <dump>/fow.json
  masks/<Scene>/fow.json   (unless --no-masks)
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mapalign import (  # noqa: E402
    _is_backdrop_tile,
    list_terrain_stems,
    load_json,
    main_tile_stem,
)
from tld_paths import dump_root  # noqa: E402

DEFAULT_MAP_RADIUS = 900.0  # FogOfWar.m_MapRadius — NOT the charcoal UV radius
# Panel_Map.MAP_RADIUS: map_bg / POI UV = ±(mapRadiusConstant / detailScale).
# Do not confuse with mapRadius (900) above.
DEFAULT_MAP_RADIUS_CONSTANT = 300.0
DEFAULT_DETAIL_SCALE = 0.925
# Matches TldevMaps RegionConfigs for the MountainPass-sized Mod clone.
DETAIL_SCALE_OVERRIDES = {
    "ModMountainPass": 1.0,
}
MOUNTAIN_PASS_EXPECTED_SR = 2121.3
VALIDATE_TOLERANCE = 1.0


def scene_name_for(dump: Path) -> str:
    meta_path = dump / "meta.json"
    if meta_path.exists():
        name = load_json(meta_path).get("sceneName")
        if name:
            return str(name)
    return dump.name


def land_stems(dump: Path, *, main_only: bool, all_tiles: bool) -> list[str]:
    """Tiles that define the FOW AABB (default: all non-water/ice land tiles)."""
    main = main_tile_stem(dump)
    if main_only:
        return [main]
    stems = list_terrain_stems(dump)
    if all_tiles:
        return stems
    out = [s for s in stems if not _is_backdrop_tile(load_json(dump / f"{s}_meta.json"))]
    return out or [main]


def tile_xz_aabb(meta: dict) -> tuple[float, float, float, float]:
    """Return (minX, maxX, minZ, maxZ) in world space."""
    bmin = meta.get("boundsMin")
    bmax = meta.get("boundsMax")
    if isinstance(bmin, dict) and isinstance(bmax, dict):
        return (
            float(bmin["x"]),
            float(bmax["x"]),
            float(bmin["z"]),
            float(bmax["z"]),
        )
    pos, size = meta["position"], meta["size"]
    x0 = float(pos["x"])
    z0 = float(pos["z"])
    return x0, x0 + float(size["x"]), z0, z0 + float(size["z"])


def union_aabb(metas: list[dict]) -> tuple[float, float, float, float]:
    boxes = [tile_xz_aabb(m) for m in metas]
    return (
        min(b[0] for b in boxes),
        max(b[1] for b in boxes),
        min(b[2] for b in boxes),
        max(b[3] for b in boxes),
    )


def half_diagonal(min_x: float, max_x: float, min_z: float, max_z: float) -> float:
    return 0.5 * math.hypot(max_x - min_x, max_z - min_z)


def detail_scale_for(scene: str, override: float | None) -> float:
    if override is not None:
        return float(override)
    return float(DETAIL_SCALE_OVERRIDES.get(scene, DEFAULT_DETAIL_SCALE))


def build_config(
    dump: Path,
    *,
    main_only: bool = False,
    all_tiles: bool = False,
    map_radius: float = DEFAULT_MAP_RADIUS,
    map_radius_constant: float = DEFAULT_MAP_RADIUS_CONSTANT,
    detail_scale: float | None = None,
) -> dict:
    scene = scene_name_for(dump)
    stems = land_stems(dump, main_only=main_only, all_tiles=all_tiles)
    metas = [load_json(dump / f"{s}_meta.json") for s in stems]
    min_x, max_x, min_z, max_z = union_aabb(metas)
    scene_radius = half_diagonal(min_x, max_x, min_z, max_z)

    main_meta = load_json(dump / f"{main_tile_stem(dump)}_meta.json")
    pos = main_meta.get("position") or {}
    size = main_meta.get("size") or {}
    terrain_y = float(pos.get("y", 0.0) or 0.0)
    terrain_max_h = float(size.get("y", 0.0) or 0.0)

    ds = detail_scale_for(scene, detail_scale)
    extent = "mainHalfDiagonal" if main_only else (
        "allTilesHalfDiagonal" if all_tiles else "unionHalfDiagonal"
    )

    return {
        "sceneName": scene,
        "sceneRadius": round(scene_radius, 4),
        "mapRadius": float(map_radius),
        "mapRadiusConstant": float(map_radius_constant),
        "detailScale": {"x": ds, "y": ds},
        "mapBackgroundFilename": f"map_bg_{scene}",
        "mapDetailsFilename": "map_details_blank",
        "heightmapFilename": "",
        "extentSource": extent,
        "terrainPositionY": terrain_y,
        "terrainMaxHeight": terrain_max_h,
        "unionBounds": {
            "minX": min_x,
            "maxX": max_x,
            "minZ": min_z,
            "maxZ": max_z,
            "tiles": stems,
        },
        "notes": [
            "mapRadiusConstant = Panel_Map.MAP_RADIUS (charcoal UV ±constant/detailScale).",
            "mapRadius = FogOfWar.m_MapRadius (usually 900); not used for map_bg framing.",
            "World→map scale ≈ mapRadiusConstant * √2 / sceneRadius (half-diagonal → half-side).",
        ],
    }


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def validate_mountain_pass(dump: Path, config: dict) -> int:
    """Optional check: computed SR should match vanilla MountainPassRegion ≈ 2121.3."""
    scene = config["sceneName"]
    sr = float(config["sceneRadius"])
    expected = MOUNTAIN_PASS_EXPECTED_SR
    fog_path = dump / "fog_of_war.json"
    fog_sr = None
    if fog_path.exists():
        fog = load_json(fog_path)
        # Only trust fog when the dictionary key matches this scene (not Ash Canyon fallback).
        key = fog.get("fogOfWarDictionaryKey") or ""
        if key and str(key).casefold() == scene.casefold():
            if fog.get("sceneRadius") is not None:
                fog_sr = float(fog["sceneRadius"])
                expected = fog_sr

    delta = abs(sr - expected)
    label = f"{scene}: sceneRadius={sr:.4f} expected≈{expected:.4f} Δ={delta:.4f}"
    if delta > VALIDATE_TOLERANCE:
        print(f"VALIDATE FAIL: {label}")
        return 1
    print(f"VALIDATE OK: {label}" + (f" (fog_of_war.json={fog_sr})" if fog_sr is not None else ""))
    return 0


def is_mod_scene(name: str) -> bool:
    return name.startswith("Mod") and not name.endswith("_Region")


def discover_mod_dumps(root: Path) -> list[Path]:
    out: list[Path] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir() or not is_mod_scene(child.name):
            continue
        if list(child.glob("terrain_*_meta.json")):
            out.append(child)
    return out


def process_dump(
    dump: Path,
    *,
    masks_root: Path | None,
    main_only: bool,
    all_tiles: bool,
    map_radius: float,
    map_radius_constant: float,
    detail_scale: float | None,
    validate: bool,
    dry_run: bool,
) -> int:
    if not dump.is_dir():
        print(f"No dump directory at {dump}")
        return 1
    if not list(dump.glob("terrain_*_meta.json")):
        print(f"No terrain_*_meta.json in {dump}")
        return 1

    config = build_config(
        dump,
        main_only=main_only,
        all_tiles=all_tiles,
        map_radius=map_radius,
        map_radius_constant=map_radius_constant,
        detail_scale=detail_scale,
    )
    scene = config["sceneName"]
    dump_out = dump / "fow.json"
    mask_out = (masks_root / scene / "fow.json") if masks_root is not None else None

    print(
        f"{scene}: sceneRadius={config['sceneRadius']} "
        f"mapRadius={config['mapRadius']} mapRadiusConstant={config['mapRadiusConstant']} "
        f"detailScale={config['detailScale']['x']} "
        f"({config['extentSource']}; tiles={', '.join(config['unionBounds']['tiles'])})"
    )

    if dry_run:
        print(json.dumps(config, indent=2))
    else:
        write_json(dump_out, config)
        print(f"  wrote {dump_out}")
        if mask_out is not None:
            write_json(mask_out, config)
            print(f"  wrote {mask_out}")

    if validate:
        return validate_mountain_pass(dump, config)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "dump_dir",
        type=Path,
        nargs="?",
        help="TerrainDumper dump folder (scene). Required unless --all-mod.",
    )
    ap.add_argument(
        "--all-mod",
        action="store_true",
        help="Process every Mod* dump under TERRAIN_DUMPER_ROOT / TLD_PATH dumps.",
    )
    ap.add_argument(
        "--dump-root",
        type=Path,
        default=None,
        help="Dump root for --all-mod (default: TERRAIN_DUMPER_ROOT / TLD_PATH).",
    )
    ap.add_argument(
        "--masks-dir",
        type=Path,
        default=None,
        help="Parent masks folder (default: <repo>/masks). Writes masks/<Scene>/fow.json.",
    )
    ap.add_argument(
        "--no-masks",
        action="store_true",
        help="Only write <dump>/fow.json (skip masks/<Scene>/fow.json).",
    )
    ap.add_argument(
        "--main-tile-only",
        action="store_true",
        help="sceneRadius from primary land tile only (not land-union).",
    )
    ap.add_argument(
        "--all-tiles",
        action="store_true",
        help="Include water/ice backdrop tiles in the AABB union.",
    )
    ap.add_argument(
        "--map-radius",
        type=float,
        default=DEFAULT_MAP_RADIUS,
        help=f"FogOfWar.m_MapRadius (default {DEFAULT_MAP_RADIUS:g}; not charcoal UV)",
    )
    ap.add_argument(
        "--map-radius-constant",
        type=float,
        default=DEFAULT_MAP_RADIUS_CONSTANT,
        help=(
            f"Panel_Map.MAP_RADIUS / mapRadiusConstant for map_bg UV "
            f"(default {DEFAULT_MAP_RADIUS_CONSTANT:g})"
        ),
    )
    ap.add_argument(
        "--detail-scale",
        type=float,
        default=None,
        help=f"Override detailScale (default {DEFAULT_DETAIL_SCALE} / ModMountainPass=1.0)",
    )
    ap.add_argument(
        "--validate",
        action="store_true",
        help="Assert sceneRadius ≈ MountainPass vanilla (2121.3) or matching fog when key==scene.",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Print config JSON; do not write files.",
    )
    args = ap.parse_args()

    if args.main_tile_only and args.all_tiles:
        print("Use only one of --main-tile-only / --all-tiles")
        return 2

    masks_root: Path | None = None
    if not args.no_masks:
        masks_root = args.masks_dir or (Path(__file__).resolve().parent.parent / "masks")

    dumps: list[Path] = []
    if args.all_mod:
        root = args.dump_root or dump_root(required=True)
        assert root is not None
        dumps = discover_mod_dumps(root)
        if not dumps:
            print(f"No Mod* dumps with terrain meta under {root}")
            return 1
    elif args.dump_dir is not None:
        dumps = [args.dump_dir]
    else:
        ap.print_help()
        return 2

    rc = 0
    for dump in dumps:
        one = process_dump(
            dump,
            masks_root=masks_root,
            main_only=bool(args.main_tile_only),
            all_tiles=bool(args.all_tiles),
            map_radius=float(args.map_radius),
            map_radius_constant=float(args.map_radius_constant),
            detail_scale=args.detail_scale,
            validate=bool(args.validate),
            dry_run=bool(args.dry_run),
        )
        if one != 0:
            rc = one
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
