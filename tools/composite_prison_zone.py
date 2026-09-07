"""Overlay full BlackrockPrisonSurvivalZone onto BlackrockRegion map UV.

Uses BlackrockRegion world→map affine (shared map_bg). Prison ortho/DEM/enrichment
are sampled in world XZ — overlap replaces outdoor pixels (prison has more detail).

Preview only.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from mapalign import (  # noqa: E402
    collect_samples,
    enrichment_structure_ids,
    fit_affine,
    load_enrichment,
    load_json,
    load_tile_heights,
    main_tile_stem,
    output_size_for_extent,
    resolve_map_extent,
    warp_dem_to_image,
    warp_label_to_image,
    warp_ortho_to_image,
    world_to_pixel,
)


def load_ortho(dump: Path) -> tuple[np.ndarray, dict]:
    # Blackrock outdoor + prison: land-first (same as make_map_bg / template).
    for stem in ("ortho_color_tiled_land", "ortho_color_tiled"):
        png = dump / f"{stem}.png"
        meta_path = dump / f"{stem}_meta.json"
        if png.is_file() and meta_path.is_file():
            return np.asarray(Image.open(png).convert("RGB"), dtype=np.uint8), load_json(meta_path)
    raise FileNotFoundError(f"no ortho in {dump}")


def charcoalish(rgb: np.ndarray) -> np.ndarray:
    """Quick desat + gentle crush so prison ortho isn't a neon blob on charcoal."""
    x = rgb.astype(np.float32)
    luma = 0.299 * x[..., 0] + 0.587 * x[..., 1] + 0.114 * x[..., 2]
    des = x * 0.25 + luma[..., None] * 0.75
    # lift shadows slightly, compress highlights
    des = (des - 20.0) * (220.0 / 235.0) + 25.0
    return np.clip(des, 0, 255)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--dump-root",
        type=Path,
        default=Path(r"I:\SteamLibrary\steamapps\common\TheLongDark\Mods\TerrainDumper"),
    )
    ap.add_argument("--size", type=int, default=4096)
    ap.add_argument("--alpha", type=float, default=1.0, help="Prison ortho blend 0..1")
    ap.add_argument("--no-charcoalish", action="store_true")
    ap.add_argument("--out-dir", type=Path, default=ROOT / "out" / "maps")
    args = ap.parse_args()

    outdoor = args.dump_root / "BlackrockRegion"
    prison = args.dump_root / "BlackrockPrisonSurvivalZone"
    base_map = args.out_dir / "map_bg_BlackrockRegion_new.png"
    if not base_map.is_file():
        raise SystemExit(f"missing {base_map}")

    # Outdoor map frame (ship map_bg UV)
    samples = load_json(outdoor / "alignment_samples.json")
    world, mapxy = collect_samples(samples, prefer_tile="terrain_01")
    A = fit_affine(world, mapxy)
    extent = resolve_map_extent(outdoor, mapxy)
    width, height = output_size_for_extent(args.size, extent, 0)
    print(f"BlackrockRegion map {width}x{height}")

    # Prison DEM footprint in that UV
    tmeters, tmeta = load_tile_heights(prison, main_tile_stem(prison))
    _tdem, tvalid = warp_dem_to_image(tmeters, tmeta, A, extent, width, height)
    print(f"prison DEM cover: {int(tvalid.sum()):,} px")

    orgb, ometa = load_ortho(prison)
    owarp, ovalid = warp_ortho_to_image(orgb, ometa, A, extent, width, height)
    paste = tvalid & ovalid
    print(f"paste (DEM&ortho): {int(paste.sum()):,} px")

    base = np.asarray(Image.open(base_map).convert("RGB"), dtype=np.float32)
    if base.shape[:2] != (height, width):
        base = np.asarray(
            Image.fromarray(base.astype(np.uint8)).resize(
                (width, height), Image.Resampling.LANCZOS
            ),
            dtype=np.float32,
        )

    src = owarp.astype(np.float32)
    if not args.no_charcoalish:
        src = charcoalish(src)

    a = float(np.clip(args.alpha, 0.0, 1.0))
    out = base.copy()
    for c in range(3):
        out[..., c] = np.where(paste, out[..., c] * (1.0 - a) + src[..., c] * a, out[..., c])

    # Structure rim from prison enrichment (alignment check vs outdoor walls)
    pload = load_enrichment(prison)
    if pload is not None:
        _, _, pmeta = pload
        if pmeta.get("classes") is not None:
            pclass, pvalid = warp_label_to_image(
                pmeta["classes"], pmeta, A, extent, width, height
            )
            pids = enrichment_structure_ids(pmeta.get("classLabels"))
            struct = paste & pvalid & np.isin(pclass, list(pids))
            if struct.any():
                ink = np.array([22.0, 20.0, 18.0], dtype=np.float32)
                for c in range(3):
                    out[..., c] = np.where(struct, out[..., c] * 0.55 + ink[c] * 0.45, out[..., c])
                print(f"prison structure tint: {int(struct.sum()):,} px")

    args.out_dir.mkdir(parents=True, exist_ok=True)
    out_path = args.out_dir / "map_bg_BlackrockRegion_prison_zone_overlay.png"
    Image.fromarray(np.clip(out, 0, 255).astype(np.uint8)).save(out_path)
    print("wrote", out_path)

    # Alignment debug: outdoor walls red, prison paste cyan edge, gate yellow
    oload = load_enrichment(outdoor)
    dbg = np.clip(out, 0, 255).astype(np.uint8).copy()
    if oload is not None:
        _, _, emeta = oload
        eclass, cvalid = warp_label_to_image(
            emeta["classes"], emeta, A, extent, width, height
        )
        oids = enrichment_structure_ids(emeta.get("classLabels"))
        ostruct = cvalid & np.isin(eclass, list(oids))
        dbg[ostruct] = (220, 40, 40)

    # Prison footprint outline
    from scipy import ndimage

    edge = paste & ~ndimage.binary_erosion(paste, iterations=2)
    dbg[edge] = (40, 220, 255)

    gate = world_to_pixel(np.array([[-186.06, 5.82]]), A, extent, width, height)[0]
    gx, gy = int(round(gate[0])), int(round(gate[1]))
    dbg[max(0, gy - 4) : gy + 5, max(0, gx - 4) : gx + 5] = (255, 255, 40)

    # Crop around prison chunk
    ys, xs = np.where(paste)
    pad = 80
    y0, y1 = max(0, int(ys.min()) - pad), min(height, int(ys.max()) + pad)
    x0, x1 = max(0, int(xs.min()) - pad), min(width, int(xs.max()) + pad)
    crop_path = args.out_dir / "debug_blackrock_prison_zone_overlay_crop.png"
    Image.fromarray(dbg[y0:y1, x0:x1]).save(crop_path)
    print("wrote", crop_path)
    print("legend: red=outdoor structure, cyan=prison DEM edge, yellow=gate")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
