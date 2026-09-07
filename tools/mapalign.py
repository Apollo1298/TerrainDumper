"""Shared world↔map affine helpers for TerrainDumper offline tools."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
from PIL import Image

# Image framing in Panel_Map space: (mx0, my0, mx1, my1)
MapExtent = tuple[float, float, float, float]


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def fit_affine(world_xz: np.ndarray, map_xy: np.ndarray) -> np.ndarray:
    n = world_xz.shape[0]
    w = np.column_stack([world_xz[:, 0], world_xz[:, 1], np.ones(n)])
    ax, _, _, _ = np.linalg.lstsq(w, map_xy[:, 0], rcond=None)
    ay, _, _, _ = np.linalg.lstsq(w, map_xy[:, 1], rcond=None)
    return np.vstack([ax, ay])


def apply_affine(A: np.ndarray, xz: np.ndarray) -> np.ndarray:
    w = np.column_stack([xz[:, 0], xz[:, 1], np.ones(xz.shape[0])])
    return w @ A.T


def invert_affine(A: np.ndarray) -> np.ndarray:
    M = np.array(
        [
            [A[0, 0], A[0, 1], A[0, 2]],
            [A[1, 0], A[1, 1], A[1, 2]],
            [0.0, 0.0, 1.0],
        ],
        dtype=np.float64,
    )
    return np.linalg.inv(M)[:2, :]


def list_terrain_stems(dump: Path) -> list[str]:
    """All usable terrain_* stems in the dump (meta.json order, else sorted globs)."""
    stems: list[str] = []
    meta_path = dump / "meta.json"
    if meta_path.exists():
        stems = [str(s) for s in (load_json(meta_path).get("tiles") or [])]
    if not stems:
        stems = sorted(p.name[: -len("_meta.json")] for p in dump.glob("terrain_*_meta.json"))
    out: list[str] = []
    for stem in stems:
        path = dump / f"{stem}_meta.json"
        if not path.exists():
            continue
        meta = load_json(path)
        if "raw" not in meta or "size" not in meta:
            continue
        out.append(stem)
    return out


def _is_backdrop_tile(meta: dict) -> bool:
    """Water/ice sheets that should lose to playable land when picking a primary tile."""
    name = str(meta.get("gameObjectName") or "").lower()
    return any(tok in name for tok in ("water", "ice", "pond", "creek"))


def main_tile_stem(dump: Path) -> str:
    """
    Stem of the playable land tile, by heightmap resolution rather than area.

    TLD's flat water/ice tiles can cover more ground than the land they surround
    (CoastalRegion's Terrain_CoastalWater is 3800x3000 at res 513, while the land
    Terrain_CoastalMain is 2671x2671 at res 2049). Ash Canyon ice overlays share
    heightmap res with land but win on area — those names are deprioritized.
    """
    best: str | None = None
    best_key: tuple[int, int, float] | None = None  # land_first, res, area
    for stem in list_terrain_stems(dump):
        meta = load_json(dump / f"{stem}_meta.json")
        land = 0 if _is_backdrop_tile(meta) else 1
        key = (
            land,
            int(meta.get("heightmapResolution", 0)),
            float(meta["size"]["x"]) * float(meta["size"]["z"]),
        )
        if best_key is None or key > best_key:
            best, best_key = stem, key

    if best is None:
        raise FileNotFoundError(f"No usable terrain_NN_meta.json in {dump}")
    return best


def collect_samples(samples: dict, prefer_tile: str | None = None) -> tuple[np.ndarray, np.ndarray]:
    pts_w, pts_m = [], []
    for s in samples.get("samples", []):
        if s.get("map") is None or s.get("world") is None:
            continue
        if prefer_tile and s.get("tile") != prefer_tile:
            continue
        pts_w.append([s["world"]["x"], s["world"]["z"]])
        pts_m.append([s["map"]["x"], s["map"]["y"]])
    if len(pts_w) < 3:
        pts_w, pts_m = [], []
        for s in samples.get("samples", []):
            if s.get("map") is None or s.get("world") is None:
                continue
            pts_w.append([s["world"]["x"], s["world"]["z"]])
            pts_m.append([s["map"]["x"], s["map"]["y"]])
    return np.asarray(pts_w, dtype=np.float64), np.asarray(pts_m, dtype=np.float64)


def load_fog(dump: Path) -> dict | None:
    path = dump / "fog_of_war.json"
    if not path.exists():
        return None
    return load_json(path)


def map_extent_from_radius(radius: float) -> MapExtent:
    """Square ±radius in Panel_Map space."""
    r = float(radius)
    return (-r, -r, r, r)


def map_extent_from_fog(fog: dict, map_radius: float | None = None) -> MapExtent | None:
    """
    Background texture UV for POIs: ±(MAP_RADIUS / detailScale).

    FogOfWar.m_DetailScale < 1 expands the map range covered by the full texture
    (LakeRegion: 300/0.925 ≈ 324). Empirically matches vanilla DetailedMaps art
    vs a ±MAP_RADIUS-only frame (~7% residual zoom).
    """
    r = map_radius if map_radius is not None else fog.get("mapRadiusConstant")
    if r is None:
        return None
    r = float(r)
    if r <= 0:
        return None
    ds = fog.get("detailScale") or {}
    dsx = float(ds.get("x", 1.0) or 1.0)
    dsy = float(ds.get("y", dsx) or dsx)
    dsx = dsx if abs(dsx) > 1e-6 else 1.0
    dsy = dsy if abs(dsy) > 1e-6 else 1.0
    hx, hy = r / dsx, r / dsy
    return (-hx, -hy, hx, hy)


def map_extent_from_samples(map_xy_samples: np.ndarray, pad: float = 0.02) -> MapExtent:
    """Legacy framing: sample AABB + pad (zooms terrain; misaligns in-game POIs)."""
    mx0, my0 = map_xy_samples.min(axis=0)
    mx1, my1 = map_xy_samples.max(axis=0)
    span_x = max(1e-6, float(mx1 - mx0))
    span_y = max(1e-6, float(my1 - my0))
    return (
        float(mx0 - pad * span_x),
        float(my0 - pad * span_y),
        float(mx1 + pad * span_x),
        float(my1 + pad * span_y),
    )


def resolve_map_extent(
    dump: Path | None = None,
    map_xy_samples: np.ndarray | None = None,
    *,
    map_radius: float | None = None,
    pad: float = 0.02,
    use_sample_extent: bool = False,
) -> MapExtent:
    """
    ±(MAP_RADIUS/detailScale) from fog_of_war.json so map_bg UV matches in-game POIs.

    Raises when fog is missing or unusable — the sample-AABB frame is never a
    silent fallback, since it ships a misaligned map. Both other framings are
    opt-in: use_sample_extent (legacy AABB) and map_radius (±R, no detailScale).
    """
    if use_sample_extent:
        if map_xy_samples is None or len(map_xy_samples) < 1:
            raise ValueError("Legacy sample framing needs alignment_samples.json")
        return map_extent_from_samples(map_xy_samples, pad=pad)

    fog = load_fog(dump) if dump is not None else None
    if fog is not None:
        ext = map_extent_from_fog(fog, map_radius=map_radius)
        if ext is not None:
            return ext
    if map_radius is not None and map_radius > 0:
        return map_extent_from_radius(map_radius)
    raise ValueError(
        "map extent needs fog_of_war.json with mapRadiusConstant (and detailScale); "
        "re-run dump_map with the charcoal map opened once in region. Override with "
        "--map-radius, or --sample-extent for the legacy (POI-misaligning) frame."
    )


def output_size_for_extent(
    width: int, extent: MapExtent, height: int = 0
) -> tuple[int, int]:
    """Pixel size covering the full fog UV rectangle with isotropic map units.

    The canvas is the UV extent (POI/fog alignment). Terrain is sampled into
    that frame only where it exists; unused pixels stay blank outside fill.
    Height defaults to width * (span_y / span_x) so a 2:1 vanilla map (Ravine)
    is 4096×2048, not a stretched square. Explicit height > 0 is an override.
    """
    w = max(1, int(width))
    if int(height) > 0:
        return w, int(height)
    mx0, my0, mx1, my1 = extent
    span_x = max(1e-6, float(mx1 - mx0))
    span_y = max(1e-6, float(my1 - my0))
    h = max(1, int(round(w * span_y / span_x)))
    return w, h


def map_xy_grid(out_w: int, out_h: int, extent: MapExtent) -> tuple[np.ndarray, np.ndarray]:
    """Pixel grid → map XY (map Y increases up; image Y increases down)."""
    mx0, my0, mx1, my1 = extent
    yy, xx = np.mgrid[0:out_h, 0:out_w]
    u = xx / max(1, out_w - 1)
    v = 1.0 - (yy / max(1, out_h - 1))
    mx = mx0 + u * (mx1 - mx0)
    my = my0 + v * (my1 - my0)
    return mx, my


def map_to_pixel(map_xy: np.ndarray, extent: MapExtent, out_w: int, out_h: int) -> np.ndarray:
    mx0, my0, mx1, my1 = extent
    u = (map_xy[:, 0] - mx0) / max(1e-6, mx1 - mx0)
    v = (map_xy[:, 1] - my0) / max(1e-6, my1 - my0)
    px = u * (out_w - 1)
    py = (1.0 - v) * (out_h - 1)
    return np.stack([px, py], axis=1)


def world_to_pixel(
    xz: np.ndarray,
    A_world_to_map: np.ndarray,
    extent: MapExtent,
    out_w: int,
    out_h: int,
) -> np.ndarray:
    return map_to_pixel(apply_affine(A_world_to_map, xz), extent, out_w, out_h)


def load_tile_heights(dump: Path, stem: str | None = None) -> tuple[np.ndarray, dict]:
    meta = load_json(dump / f"{stem or main_tile_stem(dump)}_meta.json")
    raw = np.fromfile(dump / meta["raw"]["fileName"], dtype="<u2")
    w = int(meta["raw"]["width"])
    h = int(meta["raw"]["height"])
    heights = raw.reshape((h, w)).astype(np.float64) / 65535.0
    # meters
    size_y = float(meta["size"]["y"])
    pos_y = float(meta["position"]["y"])
    meters = heights * size_y + pos_y
    return meters, meta


def load_enrichment(dump: Path) -> tuple[np.ndarray, np.ndarray, dict] | None:
    """Returns (meters[h,w], hit_mask[h,w] bool, meta) or None if missing.

    When enrichment_meta.json lists classFile, meta includes:
      classes: uint8[h,w] HitClass ids
      classLabels: list[str] ordered by id
    formatVersion 4+ may also include:
      rockKinds: uint8[h,w] RockKind ids
      rockKindLabels: list[str]
      rockSnow: uint8[h,w] 0/1 snow modifier
    """
    meta_path = dump / "enrichment_meta.json"
    if not meta_path.exists():
        return None
    meta = load_json(meta_path)
    w = int(meta["width"])
    h = int(meta["height"])
    raw = np.fromfile(dump / meta["heightsFile"], dtype="<u2").reshape((h, w))
    mask = np.fromfile(dump / meta["maskFile"], dtype=np.uint8).reshape((h, w)) > 0
    hmin = float(meta["heightMinMeters"])
    hmax = float(meta["heightMaxMeters"])
    meters = hmin + (raw.astype(np.float64) / 65535.0) * (hmax - hmin)
    meters = np.where(mask, meters, np.nan)
    # Adapt to warp_dem_to_image meta shape
    meta_adapt = {
        "position": {"x": float(meta["originX"]), "y": hmin, "z": float(meta["originZ"])},
        "size": {"x": float(meta["sizeX"]), "y": max(1e-3, hmax - hmin), "z": float(meta["sizeZ"])},
        "raw": {"width": w, "height": h},
    }
    class_file = meta.get("classFile")
    if class_file:
        class_path = dump / str(class_file)
        if class_path.exists():
            classes = np.fromfile(class_path, dtype=np.uint8).reshape((h, w))
            meta_adapt["classes"] = classes
            labels = meta.get("classLabels")
            if isinstance(labels, list) and labels:
                meta_adapt["classLabels"] = [str(x) for x in labels]
            else:
                meta_adapt["classLabels"] = [
                    "none",
                    "terrain",
                    "rock",
                    "bridge",
                    "ice_backdrop",
                    "ignore",
                    "road",
                    "path",
                    "rail",
                ]
    rock_kind_file = meta.get("rockKindFile")
    if rock_kind_file:
        rk_path = dump / str(rock_kind_file)
        if rk_path.exists():
            meta_adapt["rockKinds"] = np.fromfile(rk_path, dtype=np.uint8).reshape((h, w))
            rk_labels = meta.get("rockKindLabels")
            if isinstance(rk_labels, list) and rk_labels:
                meta_adapt["rockKindLabels"] = [str(x) for x in rk_labels]
    rock_snow_file = meta.get("rockSnowFile")
    if rock_snow_file:
        rs_path = dump / str(rock_snow_file)
        if rs_path.exists():
            meta_adapt["rockSnow"] = np.fromfile(rs_path, dtype=np.uint8).reshape((h, w))
    return meters, mask, meta_adapt


# HitClass ids from EnrichmentDump (formatVersion 3+)
# Append-only — never reorder. Labels in enrichment_meta.classLabels are authoritative.
ENRICH_CLASS_NONE = 0
ENRICH_CLASS_TERRAIN = 1
ENRICH_CLASS_ROCK = 2
ENRICH_CLASS_STRUCTURE = 3  # bridges/docks/buildings/logs; legacy dumps used "bridge" at id 3
ENRICH_CLASS_BRIDGE = ENRICH_CLASS_STRUCTURE  # alias for older code
ENRICH_CLASS_ICE_BACKDROP = 4
ENRICH_CLASS_IGNORE = 5
ENRICH_CLASS_ROAD = 6
ENRICH_CLASS_PATH = 7
ENRICH_CLASS_RAIL = 8
ENRICH_OVERLAY_CLASSES = (ENRICH_CLASS_ROCK, ENRICH_CLASS_STRUCTURE)
ENRICH_TRANSPORT_CLASSES = (ENRICH_CLASS_ROAD, ENRICH_CLASS_PATH, ENRICH_CLASS_RAIL)

# RockKind ids from EnrichmentDump (formatVersion 4+)
# Append-only — never reorder. Labels in enrichment_meta.rockKindLabels are authoritative.
ENRICH_ROCK_KIND_NONE = 0
ENRICH_ROCK_KIND_OTHER = 1
ENRICH_ROCK_KIND_CLIFF08 = 2
ENRICH_ROCK_KIND_CLIFF09 = 3
ENRICH_ROCK_KIND_ROCK07 = 4
ENRICH_ROCK_KIND_ROCK08 = 5
ENRICH_ROCK_KIND_ROCK09 = 6
ENRICH_ROCK_KIND_ROCK04 = 7
ENRICH_ROCK_KIND_ROCKMID = 8
ENRICH_ROCK_KIND_CAVEROCK = 9
ENRICH_ROCK_KIND_ICECAVEROCK = 10
ENRICH_ROCK_KIND_MINEROCK = 11
ENRICH_ROCK_KIND_GEARROCK = 12
ENRICH_ROCK_KIND_BOULDER = 13
ENRICH_ROCK_KIND_CLIFF = 14

_DEFAULT_ROCK_KIND_LABELS = [
    "none",
    "other",
    "cliff08",
    "cliff09",
    "rock07",
    "rock08",
    "rock09",
    "rock04",
    "rockmid",
    "caverock",
    "icecaverock",
    "minerock",
    "gearrock",
    "boulder",
    "cliff",
]


def enrichment_rock_kind_ids(
    rock_kind_labels: list[str] | None,
    want: set[str],
    *,
    default: tuple[int, ...] = (),
) -> set[int]:
    """Resolve RockKind label names to ids (meta labels authoritative)."""
    labels = rock_kind_labels or _DEFAULT_ROCK_KIND_LABELS
    found = {i for i, name in enumerate(labels) if name in want}
    return found if found else set(default)

_ROCK_LABELS = frozenset({"rock"})
_STRUCTURE_LABELS = frozenset({"structure", "bridge"})  # "bridge" = pre-0.9.21 dumps
_TRANSPORT_LABELS = frozenset({"road", "path", "rail"})


def enrichment_class_ids(
    class_labels: list[str] | None,
    want: set[str],
    *,
    default: tuple[int, ...],
) -> set[int]:
    if not class_labels:
        return set(default)
    return {i for i, name in enumerate(class_labels) if name in want}


def enrichment_overlay_ids(class_labels: list[str] | None = None) -> set[int]:
    """Ids that get rock/structure treatment (fill/rim + protruding contour suppress)."""
    return enrichment_class_ids(
        class_labels,
        _ROCK_LABELS | _STRUCTURE_LABELS,
        default=ENRICH_OVERLAY_CLASSES,
    )


def enrichment_transport_ids(class_labels: list[str] | None = None) -> set[int]:
    """Road/path/rail: contour suppress + structure-style rim on class footprint (no fill)."""
    return enrichment_class_ids(
        class_labels,
        _TRANSPORT_LABELS,
        default=ENRICH_TRANSPORT_CLASSES,
    )


def enrichment_road_path_ids(class_labels: list[str] | None = None) -> set[int]:
    """Alias: all transport classes that get structure-style rim (road/path/rail)."""
    return enrichment_transport_ids(class_labels)


def enrichment_rock_ids(class_labels: list[str] | None = None) -> set[int]:
    return enrichment_class_ids(class_labels, _ROCK_LABELS, default=(ENRICH_CLASS_ROCK,))


def enrichment_structure_ids(class_labels: list[str] | None = None) -> set[int]:
    return enrichment_class_ids(
        class_labels,
        _STRUCTURE_LABELS,
        default=(ENRICH_CLASS_STRUCTURE,),
    )

def load_exclusion_mask(mask_png: Path, sidecar: Path) -> tuple[np.ndarray, dict]:
    """
    Hand-painted world-space out-of-bounds mask -> (excluded[h,w] float 0/1, meta).

    Export the painted layer on its own: anything not fully transparent is excluded.

    Meta mimics a terrain tile so the result can go through warp_dem_to_image. The template
    is authored north-up (row 0 = maxZ) while warp_dem_to_image expects row 0 = minZ, so the
    rows are flipped here.
    """
    info = load_json(sidecar)
    img = Image.open(mask_png)
    want = (int(info["width"]), int(info["height"]))
    if img.size != want:
        raise ValueError(f"{mask_png.name} is {img.size[0]}x{img.size[1]}, expected {want[0]}x{want[1]}")

    if "A" in img.getbands():
        painted = np.asarray(img.getchannel("A"), dtype=np.uint8) > 0
    else:
        painted = np.asarray(img.convert("L"), dtype=np.uint8) > 0

    if not painted.any():
        raise ValueError(f"{mask_png.name} has nothing painted")
    if painted.all():
        raise ValueError(f"{mask_png.name} is painted everywhere — export the painted layer alone")

    mpp = float(info["metersPerPixel"])
    meta = {
        "position": {"x": float(info["originX"]), "y": 0.0, "z": float(info["originZ"])},
        "size": {"x": want[0] * mpp, "y": 1.0, "z": want[1] * mpp},
        "raw": {"width": want[0], "height": want[1]},
    }
    return np.flipud(painted).astype(np.float64), meta


def warp_dem_to_image(
    meters: np.ndarray,
    meta: dict,
    A_world_to_map: np.ndarray,
    map_extent: MapExtent,
    out_w: int,
    out_h: int,
    *,
    return_edge_distance: bool = False,
) -> tuple[np.ndarray, np.ndarray] | tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    Returns (dem_meters[h,w], valid_mask[h,w]) in image pixel space.
    Map Y is flipped for image coordinates.

    When return_edge_distance is true, also returns edge_distance[h,w] in source
    sample pixels: larger means the sample lands farther from the tile border.
    Overlap merges can use this to avoid creating seams from shared-edge samples.
    """
    tw, th = meters.shape[1], meters.shape[0]
    pos = meta["position"]
    size = meta["size"]

    B = invert_affine(A_world_to_map)
    mx, my = map_xy_grid(out_w, out_h, map_extent)
    flat = np.column_stack([mx.ravel(), my.ravel()])
    world = apply_affine(B, flat)
    wx = world[:, 0].reshape(out_h, out_w)
    wz = world[:, 1].reshape(out_h, out_w)

    sx = (wx - pos["x"]) / size["x"] * (tw - 1)
    sy = (wz - pos["z"]) / size["z"] * (th - 1)
    inside = (sx >= 0) & (sx <= tw - 1) & (sy >= 0) & (sy <= th - 1)

    sx0 = np.clip(np.floor(sx).astype(np.int32), 0, tw - 1)
    sy0 = np.clip(np.floor(sy).astype(np.int32), 0, th - 1)
    sx1 = np.clip(sx0 + 1, 0, tw - 1)
    sy1 = np.clip(sy0 + 1, 0, th - 1)
    fx = np.clip(sx - sx0, 0, 1)
    fy = np.clip(sy - sy0, 0, 1)
    s00 = meters[sy0, sx0]
    s10 = meters[sy0, sx1]
    s01 = meters[sy1, sx0]
    s11 = meters[sy1, sx1]
    sampled = (1 - fx) * (1 - fy) * s00 + fx * (1 - fy) * s10 + (1 - fx) * fy * s01 + fx * fy * s11
    dem = np.where(inside, sampled, np.nan)
    valid = inside & np.isfinite(dem)
    if not return_edge_distance:
        return dem, valid

    edge_distance = np.minimum.reduce([sx, (tw - 1) - sx, sy, (th - 1) - sy])
    edge_distance = np.where(valid, edge_distance, -np.inf)
    return dem, valid, edge_distance


def warp_label_to_image(
    labels: np.ndarray,
    meta: dict,
    A_world_to_map: np.ndarray,
    map_extent: MapExtent,
    out_w: int,
    out_h: int,
) -> tuple[np.ndarray, np.ndarray]:
    """Nearest-neighbor warp for categorical grids (enrichment class ids). Returns (labels, valid)."""
    tw, th = labels.shape[1], labels.shape[0]
    pos = meta["position"]
    size = meta["size"]

    B = invert_affine(A_world_to_map)
    mx, my = map_xy_grid(out_w, out_h, map_extent)
    flat = np.column_stack([mx.ravel(), my.ravel()])
    world = apply_affine(B, flat)
    wx = world[:, 0].reshape(out_h, out_w)
    wz = world[:, 1].reshape(out_h, out_w)

    sx = (wx - pos["x"]) / size["x"] * (tw - 1)
    sy = (wz - pos["z"]) / size["z"] * (th - 1)
    inside = (sx >= 0) & (sx <= tw - 1) & (sy >= 0) & (sy <= th - 1)

    sx0 = np.clip(np.rint(sx).astype(np.int32), 0, tw - 1)
    sy0 = np.clip(np.rint(sy).astype(np.int32), 0, th - 1)
    sampled = labels[sy0, sx0]
    out = np.zeros((out_h, out_w), dtype=labels.dtype)
    out[inside] = sampled[inside]
    return out, inside


def warp_ortho_to_image(
    ortho_rgb: np.ndarray,
    ortho_meta: dict,
    A_world_to_map: np.ndarray,
    map_extent: MapExtent,
    out_w: int,
    out_h: int,
    flip_z: bool = True,
) -> tuple[np.ndarray, np.ndarray]:
    """
    Sample ortho PNG (HxWx3) into map pixel space.
    Ortho covers a square of side 2*orthographicSize centered at (centerX, centerZ).
    """
    oh, ow = ortho_rgb.shape[0], ortho_rgb.shape[1]
    cx = float(ortho_meta["centerX"])
    cz = float(ortho_meta["centerZ"])
    half = float(ortho_meta["orthographicSize"])

    B = invert_affine(A_world_to_map)
    mx, my = map_xy_grid(out_w, out_h, map_extent)
    flat = np.column_stack([mx.ravel(), my.ravel()])
    world = apply_affine(B, flat)
    wx = world[:, 0].reshape(out_h, out_w)
    wz = world[:, 1].reshape(out_h, out_w)

    # map world -> ortho pixel
    su = (wx - (cx - half)) / (2 * half) * (ow - 1)
    if flip_z:
        sv = (1.0 - (wz - (cz - half)) / (2 * half)) * (oh - 1)
    else:
        sv = ((wz - (cz - half)) / (2 * half)) * (oh - 1)

    inside = (su >= 0) & (su <= ow - 1) & (sv >= 0) & (sv <= oh - 1)
    su0 = np.clip(np.floor(su).astype(np.int32), 0, ow - 1)
    sv0 = np.clip(np.floor(sv).astype(np.int32), 0, oh - 1)
    su1 = np.clip(su0 + 1, 0, ow - 1)
    sv1 = np.clip(sv0 + 1, 0, oh - 1)
    fu = np.clip(su - su0, 0, 1)[..., None]
    fv = np.clip(sv - sv0, 0, 1)[..., None]
    c00 = ortho_rgb[sv0, su0].astype(np.float64)
    c10 = ortho_rgb[sv0, su1].astype(np.float64)
    c01 = ortho_rgb[sv1, su0].astype(np.float64)
    c11 = ortho_rgb[sv1, su1].astype(np.float64)
    sampled = (1 - fu) * (1 - fv) * c00 + fu * (1 - fv) * c10 + (1 - fu) * fv * c01 + fu * fv * c11
    rgb = np.zeros((out_h, out_w, 3), dtype=np.float64)
    rgb[inside] = sampled[inside]
    return rgb, inside
