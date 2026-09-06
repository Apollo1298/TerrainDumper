#!/usr/bin/env python3
"""Stamp charcoal map icons onto mask templates (offline, no in-game dump).

Reads TLD addressable bundles via UnityPy:
  - icoMap_* sprites from NGUI Base Atlas_hd
  - MapDetail / TRIGGER_LocationLabel world positions

Writes:
  out/map_icons/*.png
  out/map_pois.json
  masks/<Scene>/template_pois.png   (does not overwrite template.png)

Usage:
  python tools/stamp_template_pois.py
  python tools/stamp_template_pois.py --scene AshCanyonRegion CanneryRegion
  python tools/stamp_template_pois.py --game-path \"I:/SteamLibrary/steamapps/common/TheLongDark\"
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

try:
    import UnityPy
except ImportError:
    print("UnityPy required: pip install UnityPy", file=sys.stderr)
    raise SystemExit(1)

REPO = Path(__file__).resolve().parent.parent
DEFAULT_GAME = Path(r"I:\SteamLibrary\steamapps\common\TheLongDark")

# Harvestables / clutter that also carry MapDetail — not charcoal named POIs.
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

# LocID → scene. Longer / more specific first. Used as hard ownership, not a guess.
LOC_SCENE_HINTS: list[tuple[str, str]] = [
    ("GAMEPLAY_BlackrockTransition", "BlackrockTransitionZone"),
    ("GAMEPLAY_EnterBlackrockTransition", "BlackrockTransitionZone"),
    ("GAMEPLAY_Blackrock", "BlackrockRegion"),
    ("SCENENAME_Blackrock", "BlackrockRegion"),
    ("GAMEPLAY_BearCreek", "CoastalRegion"),
    ("GAMEPLAY_mt", "MountainTownRegion"),
    ("GAMEPLAY_rv", "RiverValleyRegion"),
    ("GAMEPLAY_Canyon", "AshCanyonRegion"),
    ("GAMEPLAY_Ash", "AshCanyonRegion"),
    ("SCENENAME_AshMine", "AshCanyonRegion"),
    ("GAMEPLAY_EnterCrashMountain", "AshCanyonRegion"),
    ("GAMEPLAY_AF", "AirfieldRegion"),
    ("GAMEPLAY_Airfield", "AirfieldRegion"),
    ("GAMEPLAY_MountainPass", "MountainPassRegion"),
    ("GAMEPLAY_Marsh", "MarshRegion"),
    ("GAMEPLAY_Rural", "RuralRegion"),
    ("GAMEPLAY_EnterPleasantValley", "RuralRegion"),
    ("GAMEPLAY_Whale", "WhalingStationRegion"),
    ("GAMEPLAY_WhalingStation", "WhalingStationRegion"),
    ("GAMEPLAY_Tracks", "TracksRegion"),
    ("GAMEPLAY_Crash", "CrashMountainRegion"),
    ("GAMEPLAY_Lake", "LakeRegion"),
    ("GAMEPLAY_CarterHydroDam", "LakeRegion"),
    ("GAMEPLAY_CampOffice", "LakeRegion"),
    ("GAMEPLAY_TrappersHomestead", "LakeRegion"),
    ("GAMEPLAY_MysteryLake", "LakeRegion"),
    ("GAMEPLAY_Coastal", "CoastalRegion"),
    ("GAMEPLAY_Cannery", "CanneryRegion"),
    ("GAMEPLAY_Mining", "MiningRegion"),
    ("GAMEPLAY_Hub", "HubRegion"),
    ("GAMEPLAY_Hway", "HighwayTransitionZone"),
    ("GAMEPLAY_EnterHighway", "HighwayTransitionZone"),
    ("GAMEPLAY_EnterDamCave", "DamRiverTransitionZoneB"),
    ("GAMEPLAY_Dam", "DamRiverTransitionZoneB"),
    ("SCENENAME_DamTransitionZone", "DamRiverTransitionZoneB"),
    ("GAMEPLAY_Ravine", "RavineTransitionZone"),
    ("GAMEPLAY_EnterRavine", "RavineTransitionZone"),
]

# MapDetail sprites to always keep (caves, transitions, hatches, mines).
KEEP_SPRITES = {
    "icoMap_cave",
    "icoMap_transitions",
    "map_transition_map",
    "icoMap_mine",
    "icoMap_hatch",
    "icoMap_location",
    "icoMap_Generic",
}

# Atlas sprite names to crop (beyond icoMap_*).
EXTRA_ATLAS_SPRITES = {
    "map_transition_map",
    "icoMap_transitions",
}

DEFAULT_SPRITE = "icoMap_location"
FALLBACK_SPRITE = "icoMap_Generic"
TRANSITION_SPRITE = "icoMap_transitions"

# LocID substring → atlas sprite (longer / more specific first). Used when
# LocationLabel has no MapDetail sprite (otherwise everything becomes a star).
LOC_ICON_KEYWORDS: list[tuple[str, str]] = [
    ("PowerPlant", "icoMap_Powerplant"),
    ("Powerplant", "icoMap_Powerplant"),
    ("OldSubstation", "icoMap_substation"),
    ("Substation", "icoMap_substation"),
    ("Prison", "icoMap_Prison"),
    ("BrokenBridge", "icoMap_brokenBridge"),
    ("BlockedBridge", "icoMap_brokenBridge"),
    ("MuleBridge", "icoMap_brokenBridge"),
    ("NarrowBridge", "icoMap_bridge"),
    ("Bridge", "icoMap_bridge"),
    ("CreekBedCave", "icoMap_cave"),
    ("WedgeCave", "icoMap_cave"),
    ("CliffCave", "icoMap_cave"),
    ("NoRoadCave", "icoMap_cave"),
    ("CuttysCave", "icoMap_cave"),
    ("Cave", "icoMap_cave"),
    ("Mine", "icoMap_mine"),
    ("TrackableBunker", "icoMap_trackableBunker"),
    ("Hatch", "icoMap_hatch"),
    ("Bunker", "icoMap_hatch"),
    ("Bricklayer", "icoMap_stoneHut"),
    ("StoneHut", "icoMap_stoneHut"),
    ("StoneCabin", "icoMap_stoneHut"),
    ("CampOffice", "icoMap_campOffice"),
    ("Campground", "icoMap_cabin"),
    ("Cabin", "icoMap_cabin"),
    ("Trailer", "icoMap_trailer"),
    ("Farmhouse", "icoMap_farmhouse"),
    ("Farm", "icoMap_farmhouse"),
    ("Barn", "icoMap_barn"),
    ("Lookout", "icoMap_lookout"),
    ("RadioTower", "icoMap_radioTower"),
    ("Radio", "icoMap_radioTower"),
    ("CarterHydro", "icoMap_dam"),
    ("HydroDam", "icoMap_dam"),
    ("Dam", "icoMap_dam"),
    ("Church", "icoMap_church"),
    ("Quonset", "icoMap_quonset"),
    ("Hangar", "icoMap_AirfieldHangar"),
    ("ControlTower", "icoMap_AirfieldControlTower"),
    ("HuntingLodge", "icoMap_huntingLodge"),
    ("Trapper", "icoMap_trapper"),
    ("Lighthouse", "icoMap_lighthouse"),
    ("Hibernia", "icoMap_hibernia"),
    ("Riken", "icoMap_riken"),
    ("Derailment", "icoMap_derailment"),
    ("MaintenanceYard", "icoMap_maintenanceYard"),
    ("CommunityHall", "icoMap_communityHall"),
    ("Concentrator", "icoMap_MiningRegionConcentrator"),
    ("Headframe", "icoMap_MiningRegionHeadframe"),
    ("Pumphouse", "icoMap_MiningRegionPumphouse"),
    ("CougarTerritory", "icoMap_cougarDen"),
    ("Cougar", "icoMap_cougarDen"),
    ("Transition", "icoMap_transitions"),
    ("Enter", "icoMap_transitions"),
]

# Ship outside fill from make_mask_template / make_map_bg (no terrain under pixel).
SHIP_OUTSIDE_RGB = (28, 26, 24)


def bundle_dirs(game: Path) -> list[Path]:
    dirs: list[Path] = []
    main = game / "tld_Data" / "StreamingAssets" / "aa" / "StandaloneWindows64"
    if main.is_dir():
        dirs.append(main)
    dlc = game / "tld_dlc"
    if dlc.is_dir():
        for p in dlc.rglob("StandaloneWindows64"):
            if p.is_dir():
                dirs.append(p)
        # Some DLC layouts drop bundles without that folder name.
        for p in dlc.iterdir():
            if p.is_dir() and any(p.glob("*.bundle")):
                dirs.append(p)
    # de-dupe
    seen: set[Path] = set()
    out: list[Path] = []
    for d in dirs:
        r = d.resolve()
        if r not in seen:
            seen.add(r)
            out.append(r)
    return out


def follow_from(obj, pp: object):
    """Resolve a PPtr relative to obj's SerializedFile (avoids cross-CAB path_id collisions)."""
    if not isinstance(pp, dict):
        return None
    path_id = pp.get("m_PathID") or 0
    file_id = pp.get("m_FileID") or 0
    if not path_id:
        return None
    if file_id == 0:
        return obj.assets_file.objects.get(path_id)
    # External CAB reference — best-effort via externals list
    try:
        ext = obj.assets_file.externals[file_id - 1]
        ext_name = getattr(ext, "name", None) or getattr(ext, "path", None) or ""
        # Search sibling readers in the same environment
        env = getattr(obj.assets_file, "environment", None) or getattr(obj.assets_file, "parent", None)
        # Fall back: scan objects sharing this assets_file's parent env
        for other in obj.assets_file.objects.values():
            break
        # UnityPy Environment stores files by name on the env used to load
    except Exception:
        return None
    return None


def world_translation_from(transform_obj) -> tuple[float, float, float]:
    """Sum local translations up the parent chain within the same assets file."""
    x = y = z = 0.0
    cur = transform_obj
    for _ in range(128):
        if cur is None or cur.type.name != "Transform":
            break
        t = cur.read_typetree()
        p = t.get("m_LocalPosition") or {}
        x += float(p.get("x", 0.0))
        y += float(p.get("y", 0.0))
        z += float(p.get("z", 0.0))
        cur = follow_from(cur, t.get("m_Father"))
    return x, y, z


def transform_for_go(go_obj):
    """Return the Transform that belongs to this GameObject (not a child)."""
    g = go_obj.read_typetree()
    for c in g.get("m_Component") or []:
        cob = follow_from(go_obj, c.get("component", c))
        if cob is None or cob.type.name != "Transform":
            continue
        t = cob.read_typetree()
        owner = follow_from(cob, t.get("m_GameObject"))
        if owner is not None and owner.path_id == go_obj.path_id:
            return cob
    return None


def world_for_go(go_obj) -> tuple[float, float, float] | None:
    tr = transform_for_go(go_obj)
    if tr is None:
        return None
    return world_translation_from(tr)


def world_from_mb_ref(mb_obj, ref: object) -> tuple[tuple[float, float, float] | None, str]:
    """Resolve world XYZ + optional GO name from a MonoBehaviour m_GameObject PPtr."""
    target = follow_from(mb_obj, ref)
    if target is None:
        return None, ""

    if target.type.name == "Transform":
        return world_translation_from(target), ""

    if target.type.name == "GameObject":
        xyz = world_for_go(target)
        name = (target.read_typetree().get("m_Name") or "") if xyz is not None else ""
        return xyz, name

    # Component → its GameObject / Transform
    try:
        t = target.read_typetree()
    except Exception:
        return None, ""
    go = follow_from(target, t.get("m_GameObject"))
    if go is None:
        return None, ""
    if go.type.name == "Transform":
        return world_translation_from(go), ""
    if go.type.name == "GameObject":
        xyz = world_for_go(go)
        name = (go.read_typetree().get("m_Name") or "") if xyz is not None else ""
        return xyz, name
    return None, ""


def extract_atlas_icons(bundle_paths: list[Path], out_dir: Path) -> dict[str, Image.Image]:
    """Crop icoMap_* (+ transition) sprites from Base Atlas_hd (rect y is top-down)."""
    icons: dict[str, Image.Image] = {}
    candidates = [
        p
        for p in bundle_paths
        if 2_000_000 <= p.stat().st_size <= 20_000_000 and b"icoMap_" in p.read_bytes()
    ]
    for path in candidates:
        env = UnityPy.load(str(path))
        tex = None
        sprites = None
        for obj in env.objects:
            if obj.type.name == "Texture2D":
                d = obj.read()
                if d.m_Name in ("Base Atlas_hd", "Base Atlas_sd"):
                    img = d.image.convert("RGBA")
                    if tex is None or d.m_Name == "Base Atlas_hd":
                        tex = img
            elif obj.type.name == "MonoBehaviour":
                t = obj.read_typetree()
                sp = t.get("mSprites")
                if not sp or len(sp) < 100:
                    continue
                if any((s.get("name") or "").startswith("icoMap_") for s in sp if isinstance(s, dict)):
                    sprites = sp
        if tex is None or sprites is None:
            continue
        out_dir.mkdir(parents=True, exist_ok=True)
        for s in sprites:
            if not isinstance(s, dict):
                continue
            name = s.get("name") or ""
            if not (name.startswith("icoMap_") or name in EXTRA_ATLAS_SPRITES):
                continue
            x, y, w, h = int(s["x"]), int(s["y"]), int(s["width"]), int(s["height"])
            crop = tex.crop((x, y, x + w, y + h))
            icons[name] = crop
            crop.save(out_dir / f"{name}.png")
        print(f"atlas icons: {len(icons)} from {path.name}")
        break
    if not icons:
        print("WARNING: Base Atlas_hd with icoMap_* not found", file=sys.stderr)
    return icons


def should_keep(loc_id: str | None, sprite: str | None) -> bool:
    if sprite and sprite in KEEP_SPRITES:
        return True
    if sprite and sprite in SKIP_SPRITES:
        return False
    if loc_id and any(loc_id.startswith(p) for p in SKIP_LOC_PREFIXES):
        return False
    if loc_id and (
        loc_id.startswith("GAMEPLAY_")
        or loc_id.startswith("SCENENAME_")
        or "Transition" in loc_id
        or loc_id.startswith("GAMEPLAY_Enter")
    ):
        return True
    if sprite and (sprite.startswith("icoMap_") or sprite in EXTRA_ATLAS_SPRITES):
        return True
    return False


def sprite_from_loc_id(loc_id: str | None) -> str | None:
    """Map locId text to a special atlas icon when possible."""
    if not loc_id:
        return None
    for needle, sprite in LOC_ICON_KEYWORDS:
        if needle.lower() in loc_id.lower():
            return sprite
    return None


def default_sprite_for(loc_id: str | None, sprite: str | None) -> str:
    if sprite == "map_transition_map":
        return TRANSITION_SPRITE
    # Keep real MapDetail sprites; upgrade bare stars via locId.
    if sprite and sprite not in (DEFAULT_SPRITE, FALLBACK_SPRITE, ""):
        return sprite
    hinted = sprite_from_loc_id(loc_id)
    if hinted:
        return hinted
    if sprite:
        return sprite
    return DEFAULT_SPRITE


def scan_bundle_pois(path: Path, known_scenes: list[str]) -> list[dict]:
    """MB-centric: LocationLabel + MapDetail (caves/transitions) via m_GameObject."""
    raw = path.read_bytes()
    # Field names often absent as ASCII (UnityPy reconstructs typetrees). Use content hints.
    if path.stat().st_size < 500_000:
        return []
    if not (
        b"icoMap_" in raw
        or b"LocationLabel" in raw
        or b"GAMEPLAY_" in raw
        or b"SCENENAME_" in raw
        or b"map_transition_map" in raw
        or any(s.encode("ascii") in raw for s in known_scenes)
    ):
        return []

    bundle_scene = detect_bundle_scene(raw, known_scenes)

    env = UnityPy.load(str(path))
    pois: list[dict] = []
    seen_keys: set[tuple] = set()

    for obj in env.objects:
        if obj.type.name != "MonoBehaviour":
            continue
        key = (id(obj.assets_file), obj.path_id)
        if key in seen_keys:
            continue
        try:
            t = obj.read_typetree()
        except Exception:
            continue

        loc_id = None
        sprite = None
        icon_type = None
        kind = None

        if "m_LocalizedLabel" in t:
            loc_id = (t.get("m_LocalizedLabel") or {}).get("m_LocalizationID")
            kind = "location"
        if "m_LocID" in t and "m_SpriteName" in t:
            if not loc_id:
                loc_id = t.get("m_LocID")
            sprite = (t.get("m_SpriteName") or "").strip() or None
            icon_type = t.get("m_IconType")
            if kind is None:
                kind = "mapdetail"

        if kind is None:
            continue
        if not should_keep(loc_id, sprite):
            continue

        xyz, go_name = world_from_mb_ref(obj, t.get("m_GameObject"))
        if xyz is None:
            continue
        wx, wy, wz = xyz
        if abs(wx) + abs(wz) < 5.0:
            continue

        # FX hosts are wrong targets for named-location MapDetails
        if go_name.startswith("FX_") and kind == "mapdetail":
            if sprite not in ("icoMap_cave", "icoMap_transitions", "icoMap_mine", "icoMap_hatch"):
                continue
        if go_name.startswith(
            (
                "OBJ_CatTail",
                "RoseHip",
                "INTERACTIVE_Reishi",
                "INTERACTIVE_OldMans",
                "INTERACTIVE_Birch",
                "INTERACTIVE_Maple",
                "CONTAINER_",
                "CORPSE_",
            )
        ):
            if sprite not in KEEP_SPRITES:
                continue

        sprite = default_sprite_for(loc_id, sprite)
        seen_keys.add(key)
        pois.append(
            {
                "x": wx,
                "y": wy,
                "z": wz,
                "sprite": sprite,
                "locId": loc_id,
                "iconType": icon_type,
                "go": go_name,
                "kind": kind,
                "bundle": path.name,
                "bundleScene": bundle_scene,
            }
        )
    return pois


def sprite_specificity(sprite: str | None) -> int:
    s = sprite or ""
    if not s or s in (DEFAULT_SPRITE, FALLBACK_SPRITE):
        return 0
    if s in (TRANSITION_SPRITE, "map_transition_map", "icoMap_cave", "icoMap_hatch", "icoMap_mine"):
        return 2
    return 3


def dedupe_pois(pois: list[dict], cell_m: float = 20.0) -> list[dict]:
    """Keep one POI per (locId, quantized xz). Prefer special icons over stars."""
    best: dict[tuple, dict] = {}

    def score(q: dict) -> int:
        sc = sprite_specificity(q.get("sprite")) * 10
        if q.get("kind") == "location":
            sc += 2
        if q.get("kind") == "mapdetail":
            sc += 1
        return sc

    def merge(winner: dict, other: dict) -> dict:
        out = dict(winner)
        if sprite_specificity(other.get("sprite")) > sprite_specificity(out.get("sprite")):
            out["sprite"] = other["sprite"]
        if not out.get("locId") and other.get("locId"):
            out["locId"] = other["locId"]
        if not out.get("bundleScene") and other.get("bundleScene"):
            out["bundleScene"] = other["bundleScene"]
        return out

    for p in pois:
        key = (
            p.get("locId") or "",
            int(round(p["x"] / cell_m)),
            int(round(p["z"] / cell_m)),
        )
        prev = best.get(key)
        if prev is None:
            best[key] = p
        elif score(p) >= score(prev):
            best[key] = merge(p, prev)
        else:
            best[key] = merge(prev, p)
    return list(best.values())


def scene_hint_for_loc(loc_id: str | None) -> str | None:
    if not loc_id:
        return None
    for prefix, scene in LOC_SCENE_HINTS:
        if loc_id.startswith(prefix):
            return scene
    return None


def detect_bundle_scene(raw: bytes, known_scenes: list[str]) -> str | None:
    """Best-guess scene for a bundle from embedded scene-name string counts."""
    scores: list[tuple[int, str]] = []
    for s in known_scenes:
        c = raw.count(s.encode("ascii"))
        # Also count bare stem without Region/Zone suffix noise
        stem = s.replace("Region", "").replace("TransitionZone", "").replace("TransitionZoneB", "")
        if stem and stem != s:
            c += raw.count(stem.encode("ascii"))
        if c:
            scores.append((c, s))
    if b"DamRiverTransitionZone" in raw and "DamRiverTransitionZoneB" in known_scenes:
        c = raw.count(b"DamRiverTransitionZone")
        scores.append((c, "DamRiverTransitionZoneB"))
    if b"AshCanyon" in raw and "AshCanyonRegion" in known_scenes:
        scores.append((raw.count(b"AshCanyon") + 5, "AshCanyonRegion"))  # bias
    if b"Cannery" in raw and "CanneryRegion" in known_scenes:
        scores.append((raw.count(b"Cannery") + 5, "CanneryRegion"))
    if not scores:
        return None
    # Merge duplicate scene entries by max score
    merged: dict[str, int] = {}
    for c, s in scores:
        merged[s] = max(merged.get(s, 0), c)
    return sorted(merged.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]


def point_in_scene(x: float, z: float, meta: dict, pad: float = 0.0) -> bool:
    return (
        meta["originX"] - pad <= x <= meta["maxX"] + pad
        and meta["originZ"] - pad <= z <= meta["maxZ"] + pad
    )


def scene_area(meta: dict) -> float:
    return max(1.0, (meta["maxX"] - meta["originX"]) * (meta["maxZ"] - meta["originZ"]))


def scene_tokens(scene_name: str) -> list[str]:
    """Tokens that mark a locId as belonging to this scene folder."""
    tokens = [scene_name]
    stem = (
        scene_name.replace("TransitionZoneB", "")
        .replace("TransitionZone", "")
        .replace("Region", "")
    )
    if stem and stem != scene_name:
        tokens.append(stem)
    return tokens


def poi_owned_by_scene(poi: dict, scene_name: str) -> bool:
    """Paint-guide rule: only locIds that name this scene (no orphan Cabin/Hatch/etc.)."""
    loc = poi.get("locId") or ""
    if not loc:
        return False
    if scene_hint_for_loc(loc) == scene_name:
        return True
    loc_l = loc.lower()
    return any(t.lower() in loc_l for t in scene_tokens(scene_name) if len(t) >= 4)


def assign_poi_to_scene(poi: dict, metas: dict[str, dict]) -> str | None:
    """Strict ownership: named locId forces that scene (must be inside AABB) or drop.

    Generics with no scene name in the locId are not assigned to region paint guides.
    """
    loc = poi.get("locId")
    if loc and any(loc.startswith(p) for p in SKIP_LOC_PREFIXES):
        return None

    x = float(poi["x"])
    z = float(poi["z"])
    hint = scene_hint_for_loc(loc)
    if hint:
        meta = metas.get(hint)
        if meta is None:
            return None
        return hint if point_in_scene(x, z, meta, pad=0.0) else None

    # No locId hint — only keep if locId literally names exactly one known scene
    # and the point sits in that scene's AABB.
    matches = [name for name in metas if poi_owned_by_scene(poi, name)]
    if len(matches) == 1 and point_in_scene(x, z, metas[matches[0]], pad=0.0):
        return matches[0]
    return None


def world_to_pixel(
    x: float,
    z: float,
    *,
    origin_x: float,
    origin_z: float,
    max_z: float,
    mpp: float,
    width: int,
    height: int,
) -> tuple[float, float]:
    px = (x - origin_x) / mpp
    py = (max_z - z) / mpp  # row 0 = maxZ
    return px, py


def guess_sprite(loc_id: str | None, sprite: str | None, icons: dict[str, Image.Image]) -> str:
    """Prefer explicit special sprite; else locId keywords; else GAMEPLAY_/SCENENAME_ stem."""
    if sprite and sprite in icons and sprite_specificity(sprite) > 0:
        return sprite

    hinted = sprite_from_loc_id(loc_id)
    if hinted and hinted in icons:
        return hinted

    for prefix in ("GAMEPLAY_", "SCENENAME_"):
        if not loc_id or not loc_id.startswith(prefix):
            continue
        stem = loc_id[len(prefix) :]
        candidates = [
            f"icoMap_{stem}",
            f"icoMap_{stem[0].lower() + stem[1:]}" if stem else "",
            f"icoMap_{stem.lower()}",
        ]
        for c in candidates:
            if c and c in icons:
                return c
        stem_l = stem.lower()
        for name in icons:
            if name.lower().replace("icomap_", "") == stem_l:
                return name

    if sprite and sprite in icons:
        return sprite
    if DEFAULT_SPRITE in icons:
        return DEFAULT_SPRITE
    if FALLBACK_SPRITE in icons:
        return FALLBACK_SPRITE
    return sprite or DEFAULT_SPRITE


# Amber — readable on snow, rock, and ortho green/brown.
DEFAULT_ICON_RGB = (255, 196, 0)


def recolor_icon(icon: Image.Image, rgb: tuple[int, int, int]) -> Image.Image:
    """Treat atlas chalk (light + alpha) as a mask; fill with accent + thin dark outline."""
    arr = np.asarray(icon.convert("RGBA"), dtype=np.float32)
    a = arr[:, :, 3] / 255.0
    lum = arr[:, :, :3].mean(axis=2) / 255.0
    # Coverage: opaque chalk (bright) or already-dark ink
    cover = np.clip(np.maximum(a * lum, a * (1.0 - lum) * 1.2), 0.0, 1.0)
    # Soft outline: dilate coverage by 1px via max of shifted copies
    pad = np.pad(cover, 1, mode="edge")
    dil = np.maximum.reduce(
        [
            pad[1:-1, 1:-1],
            pad[:-2, 1:-1],
            pad[2:, 1:-1],
            pad[1:-1, :-2],
            pad[1:-1, 2:],
        ]
    )
    outline = np.clip(dil - cover, 0.0, 1.0)
    cr, cg, cb = rgb
    out = np.zeros((*cover.shape, 4), dtype=np.float32)
    out[:, :, 0] = cr * cover + 20.0 * outline
    out[:, :, 1] = cg * cover + 20.0 * outline
    out[:, :, 2] = cb * cover + 20.0 * outline
    out[:, :, 3] = np.clip((cover + outline) * 255.0, 0, 255)
    return Image.fromarray(out.astype(np.uint8), "RGBA")


def stamp_scene(
    template_path: Path,
    mask_meta: dict,
    pois: list[dict],
    icons: dict[str, Image.Image],
    out_path: Path,
    icon_px: int,
    icon_rgb: tuple[int, int, int] = DEFAULT_ICON_RGB,
) -> int:
    base = Image.open(template_path).convert("RGBA")
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    origin_x = float(mask_meta["originX"])
    origin_z = float(mask_meta["originZ"])
    max_z = float(mask_meta["maxZ"])
    mpp = float(mask_meta["metersPerPixel"])
    width = int(mask_meta["width"])
    height = int(mask_meta["height"])

    placed = 0
    for p in pois:
        x, z = float(p["x"]), float(p["z"])
        if not point_in_scene(x, z, mask_meta, pad=0.0):
            continue
        px, py = world_to_pixel(
            x, z, origin_x=origin_x, origin_z=origin_z, max_z=max_z, mpp=mpp, width=width, height=height
        )
        if not (0 <= px < width and 0 <= py < height):
            continue
        sprite = guess_sprite(p.get("locId"), p.get("sprite"), icons)
        icon = icons.get(sprite) or icons.get(FALLBACK_SPRITE) or icons.get(DEFAULT_SPRITE)
        if icon is None:
            draw = ImageDraw.Draw(overlay)
            r = max(3, icon_px // 3)
            draw.ellipse(
                (px - r, py - r, px + r, py + r),
                fill=(*icon_rgb, 230),
                outline=(20, 20, 20, 255),
            )
            placed += 1
            continue
        iw = icon_px
        ih = max(1, int(round(icon_px * (icon.size[1] / max(1, icon.size[0])))))
        scaled = recolor_icon(icon.resize((iw, ih), Image.Resampling.LANCZOS), icon_rgb)
        box = (int(round(px - iw / 2)), int(round(py - ih / 2)))
        overlay.alpha_composite(scaled, dest=box)
        placed += 1

    Image.alpha_composite(base, overlay).convert("RGB").save(out_path)
    return placed


def main() -> int:
    ap = argparse.ArgumentParser(description="Stamp charcoal map icons onto mask templates")
    ap.add_argument("--game-path", type=Path, default=DEFAULT_GAME)
    ap.add_argument("--masks", type=Path, default=REPO / "masks")
    ap.add_argument("--out-dir", type=Path, default=REPO / "out")
    ap.add_argument("--scene", nargs="*", default=None, help="Only these scene folder names")
    ap.add_argument("--icon-px", type=int, default=28, help="Icon width in template pixels")
    ap.add_argument(
        "--icon-color",
        type=str,
        default="255,196,0",
        help="Icon RGB as r,g,b (default amber 255,196,0)",
    )
    ap.add_argument("--rescan", action="store_true", help="Ignore cached out/map_pois.json")
    args = ap.parse_args()
    try:
        parts = [int(x.strip()) for x in args.icon_color.split(",")]
        if len(parts) != 3 or any(c < 0 or c > 255 for c in parts):
            raise ValueError
        icon_rgb = (parts[0], parts[1], parts[2])
    except ValueError:
        print(f"Bad --icon-color {args.icon_color!r}; use r,g,b", file=sys.stderr)
        return 1

    game: Path = args.game_path
    if not game.is_dir():
        print(f"Game path not found: {game}", file=sys.stderr)
        return 1

    out_dir: Path = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    icons_dir = out_dir / "map_icons"
    pois_path = out_dir / "map_pois.json"

    dirs = bundle_dirs(game)
    all_bundles = [p for d in dirs for p in sorted(d.glob("*.bundle"))]
    print(f"bundle roots: {len(dirs)}  files: {len(all_bundles)}")

    icons = extract_atlas_icons(all_bundles, icons_dir)
    if not icons and icons_dir.is_dir():
        # reuse prior crops
        for p in icons_dir.glob("icoMap_*.png"):
            icons[p.stem] = Image.open(p).convert("RGBA")
        if icons:
            print(f"loaded {len(icons)} cached icons")

    # Known scenes from masks (needed while scanning bundles).
    known_scenes = sorted(
        p.name
        for p in args.masks.iterdir()
        if p.is_dir() and (p / "mask.json").exists()
    )

    if pois_path.exists() and not args.rescan:
        pois = json.loads(pois_path.read_text(encoding="utf-8"))
        print(f"cached POIs: {len(pois)} from {pois_path}")
    else:
        pois = []
        for i, b in enumerate(all_bundles):
            if i % 40 == 0:
                print(f"scanning bundles {i}/{len(all_bundles)}...")
            try:
                pois.extend(scan_bundle_pois(b, known_scenes))
            except Exception as ex:
                print(f"  skip {b.name}: {ex}")
        pois = dedupe_pois(pois)
        pois_path.write_text(json.dumps(pois, indent=2), encoding="utf-8")
        print(f"wrote {len(pois)} POIs -> {pois_path}")

    masks_root: Path = args.masks
    scene_dirs = sorted(
        p for p in masks_root.iterdir() if p.is_dir() and (p / "mask.json").exists() and (p / "template.png").exists()
    )
    if args.scene:
        want = set(args.scene)
        scene_dirs = [p for p in scene_dirs if p.name in want]

    if not scene_dirs:
        print("No masks/<Scene>/template.png + mask.json found", file=sys.stderr)
        return 1

    metas: dict[str, dict] = {}
    for name in known_scenes:
        mj = masks_root / name / "mask.json"
        if mj.exists():
            metas[name] = json.loads(mj.read_text(encoding="utf-8"))

    by_scene: dict[str, list[dict]] = {name: [] for name in known_scenes}
    unmatched = 0
    for poi in pois:
        scene = assign_poi_to_scene(poi, metas)
        poi["scene"] = scene
        if scene is None or scene not in by_scene:
            unmatched += 1
            continue
        by_scene[scene].append(poi)
    print(
        f"assigned to regions: {sum(len(v) for v in by_scene.values())}  "
        f"unmatched/dropped: {unmatched}"
    )
    for name, lst in sorted(by_scene.items(), key=lambda kv: -len(kv[1])):
        if lst:
            print(f"  {name}: {len(lst)}")

    # Persist reassigned scenes so cached JSON stays truthful.
    pois_path.write_text(json.dumps(pois, indent=2), encoding="utf-8")
    print(f"updated scene tags -> {pois_path}")

    for scene_dir in scene_dirs:
        meta = json.loads((scene_dir / "mask.json").read_text(encoding="utf-8"))
        scene_pois = [
            p
            for p in by_scene.get(scene_dir.name, [])
            if point_in_scene(float(p["x"]), float(p["z"]), meta, pad=0.0)
            and poi_owned_by_scene(p, scene_dir.name)
        ]
        out_png = scene_dir / "template_pois.png"
        n = stamp_scene(
            scene_dir / "template.png",
            meta,
            scene_pois,
            icons,
            out_png,
            icon_px=int(args.icon_px),
            icon_rgb=icon_rgb,
        )
        print(f"{scene_dir.name}: stamped {n} icons -> {out_png}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
