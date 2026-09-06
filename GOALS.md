# TerrainDumper — Goals (historical)

> **Historical.** This was the original north-star / roadmap when the repo was greenfield. The ship pipeline and commands live in [README.md](README.md). Kept for context; do not treat phase checklists or TBD names (`DumpTerrain`, etc.) as current.

**Working name:** TerrainDumper  
**Type:** MelonLoader utility mod for *The Long Dark*  
**Purpose:** Export region terrain (and related map metadata) from a loaded scene so charcoal-style region maps can be generated offline without photogrammetry.

This document was the north star for a **new repository**. It is not a DetailedMaps feature; DetailedMaps consumes finished map textures. This tool produces the raw data those textures are built from.

---

## Problem

Hinterland’s in-game charcoal maps are low-detail. High-quality topographic/aerial backgrounds (e.g. [delta’s Topographic Maps](https://steamcommunity.com/sharedfiles/filedetails/?id=1142193220)) were made by capturing hundreds of aerial screenshots and reconstructing them with Structure-from-Motion. That process takes **10–40 hours per region** and does not scale to:

- Regions added after the original map set
- Far Territory / later DLC regions
- TLDEV (The Long Development) custom regions
- Re-exports when Hinterland changes terrain

TLD already has Unity terrain height data in memory when a region is loaded. Dumping that data removes the need for SfM.

---

## Primary goal

**Given a loaded region scene, export a reproducible terrain dump** (height data + placement + map alignment metadata) with a single console command or hotkey.

Success looks like:

1. Load Mystery Lake (or any region).
2. Run `DumpTerrain` (name TBD) from the developer console.
3. Get files under something like `Mods/TerrainDumper/<SceneName>/`.
4. Those files are enough for an offline pipeline to produce a charcoal-style `map_bg_<Region>_new.png` that aligns with vanilla fog-of-war / map icons.

---

## What we ship in this repo

| In scope | Out of scope (for this repo) |
|---|---|
| MelonLoader mod that dumps data from a loaded scene | Replacing charcoal maps in-game (that’s DetailedMaps / similar) |
| Heightmap / DEM export from Unity `Terrain` objects | Photogrammetry / COLMAP pipelines |
| Scene name, terrain tile transforms, world bounds | Full POI / loot databases |
| FogOfWar / map-background alignment metadata when available | Polished end-user map art (offline tooling can live here later or separately) |
| Support for vanilla + DLC + TLDEV scenes that load as normal regions | Wintermute-only edge cases unless they come free |
| Clear dump format + docs so others can process the data | Hosting or distributing finished map image packs |

---

## Concrete deliverables

### Phase 1 — Dump works on one known region
- MelonLoader mod project targeting current TLD + MelonLoader (align with community versions, e.g. 0.7.2+).
- Console command (via Developer Console or equivalent) to dump the **current** scene.
- Export for all active `Terrain` tiles:
  - Height samples (`GetHeights` or equivalent)
  - Resolution, size, height scale, world position/rotation
- Sidecar metadata (JSON preferred):
  - Scene name
  - Dump timestamp / game/mod version if cheap to capture
  - Per-tile bounds in world space
- Write output under a stable, documented folder layout.
- README: install, dependencies, how to dump, what each file is.

**Exit criteria:** Dump Mystery Lake (`LakeRegion`). Heights + bounds are coherent when opened in a simple viewer/script (e.g. grayscale height preview).

### Phase 2 — Map alignment
- Capture enough `FogOfWar` (or related) fields to map world XZ → charcoal map UVs / pixels.
- Document how dump space relates to vanilla `map_bg_*` textures.
- Verify against an existing DetailedMaps background (e.g. `map_bg_LakeRegion_new.png`) so contours land on the same features as the known-good map.

**Exit criteria:** A height-derived overlay lines up with the known Lake Region charcoal map within a small, documented tolerance (not necessarily pixel-perfect on day one).

### Phase 3 — Multi-region reliability
- Works across major survival regions (including multi-tile terrains).
- Document failures: scenes with little/no Unity terrain, special trees, caves-only scenes, transition zones.
- Optional: orthographic top-down color/depth capture as a secondary product (nice for “aerial” style; not required for contours).

**Exit criteria:** Dump succeeds on a checklist of vanilla regions + at least one TLDEV region if available to the maintainer.

### Phase 4 — Offline processing (optional in this repo)
- Scripts (Python or similar) that turn a dump into:
  - Contour / topographic preview
  - Charcoal-styled background suitable for DetailedMaps AssetBundles
- Keep styling configurable; do not hard-require one art look.

**Exit criteria:** One documented command produces a PNG that can be dropped into a DetailedMaps-style bundle naming scheme (`map_bg_<Name>_new`).

---

## Non-goals

- Replacing delta’s artistic process for its own sake; we want **coverage and repeatability**.
- Perfect recreation of delta’s exact maps.
- Automating Hinterland asset ripping as the primary path (AssetRipper may be explored separately; this repo is **runtime dump**).
- Shipping map replacements to players from this repo (consumer mods stay separate).
- Building a full GIS stack or interactive web map (others already do web maps; we may feed them later).

---

## Design principles

1. **Runtime over offline ripping** — Prefer data from a loaded scene so world placement and FogOfWar alignment are correct.
2. **Small, boring dumps** — Stable file formats over clever binary packs. Prefer RAW/PNG heights + JSON meta.
3. **One scene at a time** — Operator loads the region; the mod dumps what’s loaded. No need for a batch scene walker in v1.
4. **Separate from DetailedMaps** — This repo does not embed or depend on DetailedMaps’ AssetBundle. Integration is file-based.
5. **TLDEV welcome** — If a custom region loads with Unity terrain + map components, dumping it should work the same way.
6. **Do no harm** — Read-only with respect to saves and terrain; no persistent quality/setting changes unless explicitly opted into (and documented).

---

## Suggested dump layout (starting point)

```text
Mods/TerrainDumper/
  LakeRegion/
    meta.json
    terrain_00_heights.raw   # or .png / tiled
    terrain_00_meta.json
    terrain_01_heights.raw
    terrain_01_meta.json
    fog_of_war.json          # if available
    preview_height.png       # optional quick-look
```

Exact schema is an implementation detail; goals only require that layout and fields be **documented and versioned** (`formatVersion` in meta).

---

## Dependencies (expected)

- *The Long Dark* (Survival; DLC as needed for Far Territory / TLDEV prerequisites)
- MelonLoader
- Developer Console (or another agreed way to invoke dump commands)
- Optional later: Map-Maker-Tools (visual capture only; not required for height dump)

---

## Relationship to other projects

| Project | Relationship |
|---|---|
| **DetailedMaps** | Downstream consumer of finished `map_bg_*_new` textures |
| **Map-Maker-Tools** | Orthogonal; useful if we add screenshot/ortho capture |
| **delta Topographic Maps** | Inspiration / quality bar / comparison baseline for classic regions |
| **TLDEV** | Source of custom regions to dump when loaded |

---

## Success metrics

- **Time:** Dump a region in under a minute of operator time (load scene + run command), excluding first-time setup.
- **Coverage:** Ability to produce usable backgrounds for regions that have no delta map.
- **Alignment:** Dumped data can be registered to vanilla map space well enough for in-game use.
- **Repeatability:** Same scene + same game version → same dump (bit-identical or documented float tolerance).

---

## Open questions

1. Exact FogOfWar fields needed for UV/world mapping on current TLD versions.
2. Whether cliffs/rock meshes must be sampled beyond Unity terrain for acceptable charcoal maps.
3. Preferred height file format (16-bit RAW vs PNG vs NumPy) for the offline pipeline.
4. Whether Phase 4 scripts live in this repo or a sibling `terrain-dumper-tools` repo.
5. Naming: `TerrainDumper` vs `TLD-MapDump` vs something else.

---

## First milestone (repo bootstrap)

1. Create repo with this goals doc as `GOALS.md`.
2. Scaffold MelonLoader mod (net6 / current TLD Il2Cpp assemblies).
3. Implement Phase 1 dump for `LakeRegion`.
4. Add a minimal height preview script or instructions so a dump can be visually sanity-checked.

Until Phase 1 exits, do not expand into styling, AssetBundles, or multi-region polish.
