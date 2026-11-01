#!/usr/bin/env python3
"""Stamp dump_portals (+ optional dump_pois) markers onto mask templates.

Uses in-game portals.json / pois.json (accurate XYZ), not charcoal UnityPy rip.
Writes masks/<Scene>/template_portals.png — does not overwrite template.png.

Usage:
  python tools/stamp_template_portals.py --scene AirfieldRegion AshCanyonRegion
  python tools/stamp_template_portals.py --with-pois --scene MountainTownRegion
  python tools/stamp_template_portals.py --dump-root \"$TERRAIN_DUMPER_ROOT\"
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tools"))
from tld_paths import resolve_dump_root  # noqa: E402

# TransitionContact / region doors vs interior load triggers vs named POIs.
TRANSITION_RGB = (0, 210, 255)  # cyan
INTERIOR_RGB = (255, 196, 0)  # amber
POI_RGB = (255, 80, 200)  # magenta
DEFAULT_MARKER_R = 4

# Keep in sync with PoisDump.cs clutter filters.
SKIP_SPRITES = {
    "icoMap_corpse",
    "icoMap_deerCarcass",
    "icoMap_wolfCarcass",
    "icoMap_bearCarcass",
    "icoMap_CougarCarcass",
    "icoMap_container",
    "ico_Radial_pack",
    "icoMap_cattails",
    "icoMap_rosehips",
    "icoMap_oldmansbeard",
    "icoMap_reishi",
    "icoMap_sapling",
    "icoMap_burdock",
    "icoMap_limb",
    "icoMap_ptarmiganNest",
    "icoMap_coal",
    "icoMap_bark",
    "icoMap_rabbit",
    "icoMap_car",
    "ico_collections_polaroids",
}
SKIP_LOC_PREFIXES = (
    "GAMEPLAY_BackPack",
    "GAMEPLAY_Corpse",
    "GAMEPLAY_FrozenCorpse",
    "GAMEPLAY_DeerCarcass",
    "GAMEPLAY_WolfCarcass",
    "GAMEPLAY_MetalContainer",
    "GAMEPLAY_PlasticContainer",
    "GAMEPLAY_DecorationBox",
    "GAMEPLAY_Climb",
    "GAMEPLAY_Cattail",
    "GAMEPLAY_RoseHip",
    "GAMEPLAY_OldMansBeard",
    "GAMEPLAY_BirchSapling",
    "GAMEPLAY_MapleSapling",
    "GAMEPLAY_Burdock",
    "GAMEPLAY_Reishi",
    "GAMEPLAY_RabbitGrove",
    "GAMEPLAY_Vehicle",
    "GAMEPLAY_VisorNote",
)
# Already drawn as cyan portals — do not double-stamp as magenta POIs.
SKIP_TRANSITION_SPRITES = {
    "icoMap_transitions",
    "map_transition_map",
}
KEEP_SPRITES = {
    "icoMap_cave",
    "icoMap_transitions",
    "map_transition_map",
    "icoMap_mine",
    "icoMap_hatch",
    "icoMap_location",
    "icoMap_Generic",
}


def load_dump_root(cli: Path | None) -> Path:
    return resolve_dump_root(cli)


def world_to_pixel(
    x: float,
    z: float,
    *,
    origin_x: float,
    origin_z: float,
    max_z: float,
    mpp: float,
) -> tuple[float, float]:
    px = (x - origin_x) / mpp
    py = (max_z - z) / mpp  # row 0 = maxZ
    return px, py


def point_in_bounds(x: float, z: float, meta: dict) -> bool:
    return meta["originX"] <= x <= meta["maxX"] and meta["originZ"] <= z <= meta["maxZ"]


def is_transition(portal: dict) -> bool:
    if portal.get("transitionOnContact") is True:
        return True
    go = (portal.get("gameObject") or "").lower()
    return "transition" in go


def is_clutter_poi(poi: dict) -> bool:
    sprite = (poi.get("sprite") or "").strip()
    loc = (poi.get("locId") or "").strip()
    go = (poi.get("gameObject") or "").strip()

    if sprite in SKIP_TRANSITION_SPRITES:
        return True
    if sprite and sprite not in KEEP_SPRITES and sprite in SKIP_SPRITES:
        return True
    if loc and any(loc.startswith(p) for p in SKIP_LOC_PREFIXES):
        return True
    if go.startswith("FX_") and sprite not in KEEP_SPRITES:
        return True
    return False


def draw_marker(
    draw: ImageDraw.ImageDraw,
    *,
    cx: int,
    cy: int,
    r: int,
    rgb: tuple[int, int, int],
    outline_w: int,
) -> None:
    draw.ellipse(
        (cx - r, cy - r, cx + r, cy + r),
        fill=(*rgb, 230),
        outline=(20, 20, 20, 255),
        width=outline_w,
    )


def stamp_scene(
    template_path: Path,
    mask_meta: dict,
    portals: list[dict],
    out_path: Path,
    *,
    marker_r: int,
    pois: list[dict] | None = None,
) -> tuple[int, int, int]:
    base = Image.open(template_path).convert("RGBA")
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    origin_x = float(mask_meta["originX"])
    origin_z = float(mask_meta["originZ"])
    max_z = float(mask_meta["maxZ"])
    mpp = float(mask_meta["metersPerPixel"])
    width = int(mask_meta["width"])
    height = int(mask_meta["height"])
    outline_w = 1 if marker_r <= 4 else 2

    n_trans = 0
    n_int = 0
    for p in portals:
        x, z = float(p["x"]), float(p["z"])
        if not point_in_bounds(x, z, mask_meta):
            continue
        px, py = world_to_pixel(x, z, origin_x=origin_x, origin_z=origin_z, max_z=max_z, mpp=mpp)
        if not (0 <= px < width and 0 <= py < height):
            continue

        trans = is_transition(p)
        rgb = TRANSITION_RGB if trans else INTERIOR_RGB
        if trans:
            n_trans += 1
        else:
            n_int += 1

        cx, cy = int(round(px)), int(round(py))
        draw_marker(draw, cx=cx, cy=cy, r=marker_r, rgb=rgb, outline_w=outline_w)

    n_poi = 0
    if pois:
        for p in pois:
            if is_clutter_poi(p):
                continue
            x, z = float(p["x"]), float(p["z"])
            if not point_in_bounds(x, z, mask_meta):
                continue
            px, py = world_to_pixel(x, z, origin_x=origin_x, origin_z=origin_z, max_z=max_z, mpp=mpp)
            if not (0 <= px < width and 0 <= py < height):
                continue
            cx, cy = int(round(px)), int(round(py))
            draw_marker(draw, cx=cx, cy=cy, r=marker_r, rgb=POI_RGB, outline_w=outline_w)
            n_poi += 1

    Image.alpha_composite(base, overlay).convert("RGB").save(out_path)
    return n_trans, n_int, n_poi


def main() -> int:
    ap = argparse.ArgumentParser(description="Stamp portals.json onto mask templates")
    ap.add_argument("--dump-root", type=Path, default=None)
    ap.add_argument("--masks", type=Path, default=REPO / "masks")
    ap.add_argument("--scene", nargs="*", default=None, help="Only these scene folder names")
    ap.add_argument(
        "--marker-r",
        type=int,
        default=DEFAULT_MARKER_R,
        help=f"Marker radius in template pixels (default {DEFAULT_MARKER_R})",
    )
    ap.add_argument(
        "--with-pois",
        action="store_true",
        help="Also stamp named POIs from dump pois.json (magenta; requires dump_pois)",
    )
    args = ap.parse_args()

    dump_root = load_dump_root(args.dump_root)
    if not dump_root.is_dir():
        print(f"Dump root not found: {dump_root}", file=sys.stderr)
        return 1

    masks_root: Path = args.masks
    scene_dirs = sorted(
        p
        for p in masks_root.iterdir()
        if p.is_dir() and (p / "mask.json").is_file() and (p / "template.png").is_file()
    )
    if args.scene:
        want = set(args.scene)
        scene_dirs = [p for p in scene_dirs if p.name in want]

    if not scene_dirs:
        print("No masks/<Scene>/template.png + mask.json found", file=sys.stderr)
        return 1

    print(f"dump root: {dump_root}")
    legend = f"cyan = region transitions  amber = interiors  (no text; r={args.marker_r})"
    if args.with_pois:
        legend += "  magenta = named POIs"
    print(legend)

    for scene_dir in scene_dirs:
        portals_path = dump_root / scene_dir.name / "portals.json"
        if not portals_path.is_file():
            print(f"{scene_dir.name}: skip (no portals.json)")
            continue
        doc = json.loads(portals_path.read_text(encoding="utf-8"))
        portals = list(doc.get("portals") or [])
        meta = json.loads((scene_dir / "mask.json").read_text(encoding="utf-8"))

        pois: list[dict] | None = None
        if args.with_pois:
            pois_path = dump_root / scene_dir.name / "pois.json"
            if not pois_path.is_file():
                print(f"{scene_dir.name}: warning — no pois.json (run dump_pois in-game)")
                pois = []
            else:
                pois = list(json.loads(pois_path.read_text(encoding="utf-8")).get("pois") or [])

        out_png = scene_dir / "template_portals.png"
        n_trans, n_int, n_poi = stamp_scene(
            scene_dir / "template.png",
            meta,
            portals,
            out_png,
            marker_r=int(args.marker_r),
            pois=pois,
        )
        extra = f", pois={n_poi}" if args.with_pois else ""
        print(
            f"{scene_dir.name}: stamped {n_trans + n_int}/{len(portals)} "
            f"(transitions={n_trans}, interiors={n_int}{extra}) -> {out_png}"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
