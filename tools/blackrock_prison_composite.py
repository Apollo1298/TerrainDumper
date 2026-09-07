"""BlackrockRegion ← BlackrockPrisonSurvivalZone yard paste (precision rule).

Constants and paste helper used by make_map_bg and composite_prison_into_blackrock.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image
from scipy import ndimage

import make_map_bg as mm
from mapalign import (
    MapExtent,
    enrichment_rock_ids,
    enrichment_structure_ids,
    enrichment_transport_ids,
    load_enrichment,
    load_json,
    load_tile_heights,
    main_tile_stem,
    warp_dem_to_image,
    warp_label_to_image,
    warp_ortho_to_image,
)

OUTDOOR_SCENE = "BlackrockRegion"
PRISON_SCENE = "BlackrockPrisonSurvivalZone"
LAND_ORTHO_SCENES = frozenset({OUTDOOR_SCENE, PRISON_SCENE})

# Tuned paste defaults (BlackrockRegion only).
CANVAS_INSET_M = 24.0
FEATHER_M = 16.0


def prefers_land_ortho(scene: str | None) -> bool:
    return bool(scene) and scene in LAND_ORTHO_SCENES


def default_yard_mask(repo_root: Path) -> Path:
    return repo_root / "masks" / PRISON_SCENE / "mask.png"


def resolve_prison_dump(outdoor_dump: Path) -> Path:
    return outdoor_dump.parent / PRISON_SCENE


@dataclass
class PrisonPasteResult:
    dem: np.ndarray
    valid: np.ndarray
    display_valid: np.ndarray
    enrich_cover: np.ndarray | None
    enrich_outline: np.ndarray | None
    enrich_outline_structure: np.ndarray | None
    enrich_rock_fill: np.ndarray | None
    enrich_structure_fill: np.ndarray | None
    enrich_rock_kinds: np.ndarray | None
    enrich_rock_snow: np.ndarray | None
    enrich_rock_kind_labels: list[str] | None
    enrich_h_for_punch: np.ndarray | None
    enrich_class_for_punch: np.ndarray | None
    # For ortho blend after outdoor ortho load.
    alpha: np.ndarray
    prison_dump: Path


def load_ortho_land_first(dump: Path) -> tuple[np.ndarray, dict]:
    for stem in ("ortho_color_tiled_land", "ortho_color_tiled"):
        png = dump / f"{stem}.png"
        meta = dump / f"{stem}_meta.json"
        if png.is_file() and meta.is_file():
            print(f"ortho {dump.name}: {stem}.png")
            return np.asarray(Image.open(png).convert("RGB"), dtype=np.uint8), load_json(meta)
    raise FileNotFoundError(f"no ortho in {dump}")


def blend_ortho_with_prison(
    owarp: np.ndarray,
    ovalid: np.ndarray,
    *,
    prison_dump: Path,
    A: np.ndarray,
    extent: MapExtent,
    width: int,
    height: int,
    alpha: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """Blend outdoor ortho with prison land ortho using paste alpha."""
    prgb, pometa = load_ortho_land_first(prison_dump)
    pwarp, povalid = warp_ortho_to_image(prgb, pometa, A, extent, width, height)
    out = np.asarray(owarp, dtype=np.float64).copy()
    pwarp = pwarp.astype(np.float64)
    both = ovalid & povalid
    only_p = povalid & ~ovalid
    for c in range(3):
        blended = out[..., c] * (1.0 - alpha) + pwarp[..., c] * alpha
        out[..., c] = np.where(both, blended, out[..., c])
        out[..., c] = np.where(only_p & (alpha > 0.01), pwarp[..., c], out[..., c])
    ovalid_out = ovalid | (povalid & (alpha > 0.01))
    print(
        f"prison ortho blend: both={int(both.sum()):,} "
        f"prison-only={int((only_p & (alpha > 0.01)).sum()):,}"
    )
    return out, ovalid_out


def _enrich_layers_at(
    emeters: np.ndarray,
    emeta: dict,
    dem_ref: np.ndarray,
    valid_ref: np.ndarray,
    *,
    A: np.ndarray,
    extent: MapExtent,
    width: int,
    height: int,
):
    edem, evalid = warp_dem_to_image(emeters, emeta, A, extent, width, height)
    both = valid_ref & evalid & np.isfinite(edem) & np.isfinite(dem_ref)
    delta = np.where(both, edem - dem_ref, 0.0)
    dem_out = dem_ref.copy()
    dem_out = np.where(both, np.maximum(dem_ref, edem), dem_ref)
    scale = max(1.0, float(max(width, height)) / 2048.0)
    classes = emeta.get("classes")
    if classes is None:
        cover = both & (delta > mm.ENRICH_PROTRUDE_M)
        enrich_h = np.where(evalid & np.isfinite(edem), edem, np.nan)
        return (
            dem_out,
            cover,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            enrich_h,
            None,
        )

    eclass, cvalid = warp_label_to_image(classes, emeta, A, extent, width, height)
    labels = emeta.get("classLabels")
    rock_ids = enrichment_rock_ids(labels)
    struct_ids = enrichment_structure_ids(labels)
    transport_ids = enrichment_transport_ids(labels)
    rock = cvalid & np.isin(eclass, list(rock_ids))
    structure = cvalid & np.isin(eclass, list(struct_ids))
    transport = (
        cvalid & np.isin(eclass, list(transport_ids))
        if transport_ids
        else np.zeros_like(cvalid, dtype=bool)
    )
    overlay = rock | structure
    protrude = both & (delta > mm.ENRICH_PROTRUDE_M)
    cover = (overlay & protrude) | transport
    rock_fill = rock.copy()
    struct_fill = structure | transport
    outside = evalid & ~valid_ref
    rock_rim = mm.enrichment_rim_mask(rock & (protrude | outside), scale=scale)
    rock_rim = mm.strip_rock_rim_at_structure(
        rock_rim, structure | transport, scale=scale
    )
    struct_rim = mm.enrichment_rim_mask(
        (structure & (protrude | outside)) | transport,
        scale=scale,
        dilate_scale=0.0,
    )

    kinds = None
    snow = None
    kind_labels = None
    rock_kinds = emeta.get("rockKinds")
    if rock_kinds is not None:
        rk, rk_valid = warp_label_to_image(rock_kinds, emeta, A, extent, width, height)
        kinds = np.where(rk_valid, rk.astype(np.uint8), np.uint8(0))
        kind_labels = [str(x) for x in (emeta.get("rockKindLabels") or [])]
    rock_snow = emeta.get("rockSnow")
    if rock_snow is not None:
        rs, rs_valid = warp_label_to_image(rock_snow, emeta, A, extent, width, height)
        snow = np.where(rs_valid, rs.astype(np.uint8), np.uint8(0))

    enrich_h = np.where(evalid & np.isfinite(edem), edem, np.nan)
    enrich_class = np.where(cvalid, eclass.astype(np.int16), np.int16(-1))
    return (
        dem_out,
        cover,
        rock_rim,
        struct_rim,
        rock_fill,
        struct_fill,
        kinds,
        snow,
        kind_labels,
        enrich_h,
        enrich_class,
    )


def _mix_bool(a: np.ndarray | None, b: np.ndarray | None, hole_hard: np.ndarray, shape: tuple[int, int]):
    if a is None and b is None:
        return None
    if a is None:
        a = np.zeros(shape, dtype=bool)
    if b is None:
        b = np.zeros(shape, dtype=bool)
    return np.where(hole_hard, b, a)


def _mix_u8(a: np.ndarray | None, b: np.ndarray | None, hole_hard: np.ndarray, shape: tuple[int, int]):
    if a is None and b is None:
        return None
    if a is None:
        a = np.zeros(shape, dtype=np.uint8)
    if b is None:
        b = np.zeros(shape, dtype=np.uint8)
    return np.where(hole_hard, b, a).astype(np.uint8)


def try_apply_prison_paste(
    *,
    outdoor_dump: Path,
    repo_root: Path,
    A: np.ndarray,
    extent: MapExtent,
    width: int,
    height: int,
    dem_outdoor: np.ndarray,
    valid_outdoor: np.ndarray,
    display_valid: np.ndarray,
    yard_mask: Path | None = None,
    full_dem_hole: bool = False,
    canvas_inset_m: float = CANVAS_INSET_M,
    feather_m: float = FEATHER_M,
) -> PrisonPasteResult | None:
    """Paste prison yard into outdoor Blackrock grids. Returns None if dumps/mask missing."""
    prison = resolve_prison_dump(outdoor_dump)
    if not (prison / "meta.json").is_file():
        print(f"Blackrock prison paste: skip (no dump at {prison})")
        return None

    dem_outdoor = dem_outdoor.copy()
    valid_outdoor = valid_outdoor.copy()
    display_valid = display_valid.copy()

    pmeters, pmeta = load_tile_heights(prison, main_tile_stem(prison))
    pdem, pvalid = warp_dem_to_image(pmeters, pmeta, A, extent, width, height)
    dem_hole = pvalid & np.isfinite(pdem)

    if full_dem_hole:
        hole = dem_hole
        print(f"prison hole (full DEM): {int(hole.sum()):,} px")
    else:
        yard_png = yard_mask or default_yard_mask(repo_root)
        yard_json = yard_png.with_name("mask.json")
        if not yard_png.is_file() or not yard_json.is_file():
            print(f"Blackrock prison paste: skip (yard mask missing: {yard_png})")
            return None
        painted, mask_meta = mm.load_exclusion_mask(yard_png, yard_json)
        excluded_w, excl_valid = warp_dem_to_image(
            painted, mask_meta, A, extent, width, height
        )
        keep = excl_valid & (excluded_w < 0.5)
        hole = dem_hole & keep
        print(
            f"prison hole (hand mask keep): {int(hole.sum()):,} px "
            f"(DEM={int(dem_hole.sum()):,}, keep_warped={int(keep.sum()):,})"
        )

    if canvas_inset_m > 0 and hole.any() and dem_hole.any():
        dz, dx = mm.world_pixel_size(A, extent, dem_hole.shape)
        dist_from_canvas = ndimage.distance_transform_edt(dem_hole, sampling=(dz, dx))
        before = int(hole.sum())
        hole = hole & (dist_from_canvas >= canvas_inset_m)
        print(
            f"canvas inset {canvas_inset_m:g} m from prison DEM edge: "
            f"{before:,} -> {int(hole.sum()):,} px"
        )

    if feather_m > 0 and hole.any():
        dz, dx = mm.world_pixel_size(A, extent, hole.shape)
        dist_in = ndimage.distance_transform_edt(hole, sampling=(dz, dx))
        dist_out = ndimage.distance_transform_edt(~hole, sampling=(dz, dx))
        signed = dist_in - dist_out
        alpha = np.clip((signed + 0.5 * feather_m) / feather_m, 0.0, 1.0).astype(np.float64)
        print(
            f"paste feather {feather_m:g} m: "
            f"alpha>0.99={int((alpha > 0.99).sum()):,} "
            f"blend={int(((alpha > 0.01) & (alpha < 0.99)).sum()):,}"
        )
    else:
        alpha = hole.astype(np.float64)

    oloaded = load_enrichment(outdoor_dump)
    ploaded = load_enrichment(prison)
    if oloaded is None or ploaded is None:
        print("Blackrock prison paste: skip (enrichment missing on outdoor or prison)")
        return None
    oemeters, _, oemeta = oloaded
    pemeters, _, pemeta = ploaded

    dem_o, cover_o, rim_o, srim_o, rock_o, struct_o, kinds_o, snow_o, labels_o, eh_o, ec_o = (
        _enrich_layers_at(
            oemeters,
            oemeta,
            dem_outdoor,
            valid_outdoor,
            A=A,
            extent=extent,
            width=width,
            height=height,
        )
    )
    dem_p, cover_p, rim_p, srim_p, rock_p, struct_p, kinds_p, snow_p, labels_p, eh_p, ec_p = (
        _enrich_layers_at(
            pemeters,
            pemeta,
            np.where(dem_hole, pdem, np.nan),
            dem_hole,
            A=A,
            extent=extent,
            width=width,
            height=height,
        )
    )

    dem = np.where(
        np.isfinite(dem_p) & np.isfinite(dem_o),
        dem_o * (1.0 - alpha) + dem_p * alpha,
        np.where(alpha >= 0.5, dem_p, dem_o),
    )
    valid = valid_outdoor | (dem_hole & (alpha > 0.01))
    display_valid = display_valid | (dem_hole & (alpha > 0.01))
    hole_hard = alpha >= 0.5
    shape = (height, width)

    enrich_cover = _mix_bool(cover_o, cover_p, hole_hard, shape)
    enrich_outline = _mix_bool(rim_o, rim_p, hole_hard, shape)
    enrich_outline_structure = _mix_bool(srim_o, srim_p, hole_hard, shape)
    enrich_rock_fill = _mix_bool(rock_o, rock_p, hole_hard, shape)
    enrich_structure_fill = _mix_bool(struct_o, struct_p, hole_hard, shape)
    enrich_rock_kinds = _mix_u8(kinds_o, kinds_p, hole_hard, shape)
    enrich_rock_snow = _mix_u8(snow_o, snow_p, hole_hard, shape)
    # Prefer outdoor kind labels (shared RockKind enum); fall back to prison.
    enrich_rock_kind_labels = labels_o or labels_p

    enrich_h = None
    if eh_o is not None or eh_p is not None:
        if eh_o is None:
            eh_o = np.full(shape, np.nan)
        if eh_p is None:
            eh_p = np.full(shape, np.nan)
        enrich_h = np.where(hole_hard, eh_p, eh_o)

    enrich_class = None
    if ec_o is not None or ec_p is not None:
        if ec_o is None:
            ec_o = np.full(shape, np.int16(-1))
        if ec_p is None:
            ec_p = np.full(shape, np.int16(-1))
        enrich_class = np.where(hole_hard, ec_p, ec_o)

    print(
        f"prison paste enrich: cover={int(enrich_cover.sum()) if enrich_cover is not None else 0} "
        f"rock_fill={int(enrich_rock_fill.sum()) if enrich_rock_fill is not None else 0} "
        f"struct_fill={int(enrich_structure_fill.sum()) if enrich_structure_fill is not None else 0} "
        f"kinds={'yes' if enrich_rock_kinds is not None else 'no'}"
    )

    return PrisonPasteResult(
        dem=dem,
        valid=valid,
        display_valid=display_valid,
        enrich_cover=enrich_cover,
        enrich_outline=enrich_outline,
        enrich_outline_structure=enrich_outline_structure,
        enrich_rock_fill=enrich_rock_fill,
        enrich_structure_fill=enrich_structure_fill,
        enrich_rock_kinds=enrich_rock_kinds,
        enrich_rock_snow=enrich_rock_snow,
        enrich_rock_kind_labels=enrich_rock_kind_labels,
        enrich_h_for_punch=enrich_h,
        enrich_class_for_punch=enrich_class,
        alpha=alpha,
        prison_dump=prison,
    )
