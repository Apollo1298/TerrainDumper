#!/usr/bin/env python3
"""Stamp dump_portals LoadScene markers onto mask templates.

Uses in-game portals.json (accurate XYZ + toScene), not charcoal POI rip.
Writes masks/<Scene>/template_portals.png — does not overwrite template.png.

Usage:
  python tools/stamp_template_portals.py --scene AirfieldRegion AshCanyonRegion
  python tools/stamp_template_portals.py --dump-root \"$TERRAIN_DUMPER_ROOT\"
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tools"))
from tld_paths import resolve_dump_root  # noqa: E402

CFG_PATH = REPO / "tools" / "pathfinding_config.json"

# TransitionContact / region doors vs interior load triggers.
TRANSITION_RGB = (0, 210, 255)  # cyan
INTERIOR_RGB = (255, 196, 0)  # amber (matches prior POI stamp)


def load_dump_root(cli: Path | None) -> Path:
    config_value = None
    if CFG_PATH.is_file():
        try:
            config_value = json.loads(CFG_PATH.read_text(encoding="utf-8")).get("dumpRoot")
        except (OSError, json.JSONDecodeError):
            pass
    return resolve_dump_root(cli, config_value=config_value)


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


def short_label(to_scene: str | None) -> str:
    if not to_scene:
        return "?"
    s = to_scene
    for suffix in ("Region", "TransitionZoneB", "TransitionZone", "Cave"):
        if s.endswith(suffix) and len(s) > len(suffix) + 2:
            # keep Cave as part of name for AshCaveA; only strip Region/Zone
            if suffix == "Cave":
                break
            s = s[: -len(suffix)]
            break
    return s[:18]


def try_font(size: int) -> ImageFont.ImageFont | ImageFont.FreeTypeFont:
    for name in ("arial.ttf", "segoeui.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def stamp_scene(
    template_path: Path,
    mask_meta: dict,
    portals: list[dict],
    out_path: Path,
    *,
    marker_r: int,
) -> tuple[int, int]:
    base = Image.open(template_path).convert("RGBA")
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    font = try_font(max(11, marker_r))

    origin_x = float(mask_meta["originX"])
    origin_z = float(mask_meta["originZ"])
    max_z = float(mask_meta["maxZ"])
    mpp = float(mask_meta["metersPerPixel"])
    width = int(mask_meta["width"])
    height = int(mask_meta["height"])

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

        r = marker_r + (2 if trans else 0)
        cx, cy = int(round(px)), int(round(py))
        draw.ellipse(
            (cx - r, cy - r, cx + r, cy + r),
            fill=(*rgb, 230),
            outline=(20, 20, 20, 255),
            width=2,
        )
        if trans:
            # Inner ring so region doors stand out when painting masks.
            draw.ellipse(
                (cx - r // 2, cy - r // 2, cx + r // 2, cy + r // 2),
                outline=(20, 20, 20, 255),
                width=1,
            )

        label = short_label(p.get("toScene"))
        # Shadow then fill for readability on ortho.
        tx, ty = cx + r + 3, cy - r
        for ox, oy, col in ((1, 1, (0, 0, 0, 220)), (0, 0, (*rgb, 255))):
            draw.text((tx + ox, ty + oy), label, font=font, fill=col)

    Image.alpha_composite(base, overlay).convert("RGB").save(out_path)
    return n_trans, n_int


def main() -> int:
    ap = argparse.ArgumentParser(description="Stamp portals.json onto mask templates")
    ap.add_argument("--dump-root", type=Path, default=None)
    ap.add_argument("--masks", type=Path, default=REPO / "masks")
    ap.add_argument("--scene", nargs="*", default=None, help="Only these scene folder names")
    ap.add_argument("--marker-r", type=int, default=7, help="Marker radius in template pixels")
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
    print(f"cyan = TransitionContact / onContact  amber = interior doors")

    for scene_dir in scene_dirs:
        portals_path = dump_root / scene_dir.name / "portals.json"
        if not portals_path.is_file():
            print(f"{scene_dir.name}: skip (no portals.json)")
            continue
        doc = json.loads(portals_path.read_text(encoding="utf-8"))
        portals = list(doc.get("portals") or [])
        meta = json.loads((scene_dir / "mask.json").read_text(encoding="utf-8"))
        out_png = scene_dir / "template_portals.png"
        n_trans, n_int = stamp_scene(
            scene_dir / "template.png",
            meta,
            portals,
            out_png,
            marker_r=int(args.marker_r),
        )
        print(
            f"{scene_dir.name}: stamped {n_trans + n_int}/{len(portals)} "
            f"(transitions={n_trans}, interiors={n_int}) -> {out_png}"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
