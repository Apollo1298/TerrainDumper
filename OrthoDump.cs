using System.Collections;
using System.Globalization;
using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;
using MelonLoader;
using MelonLoader.Utils;
using UnityEngine;
using UnitySceneManager = UnityEngine.SceneManagement.SceneManager;

namespace TerrainDumper;

/// <summary>
/// Orthographic top-down capture via a dedicated temp camera + RenderTexture (not the gameplay camera).
/// Loaded only when dump_* ortho runs — never at boot.
/// </summary>
internal static class OrthoDump
{
    private const int DefaultResolution = 2048;
    private const int DefaultTileGrid = 8;
    private const int DefaultTileRes = 1024;
    private const int MinTileGrid = 2;
    private const int MaxTileGrid = 8;
    private const int MinTileRes = 512;
    private const int MaxTileRes = 2048;

    private static bool _busy;

    public static bool IsTiledRunning => _busy;

    public static void RunFromConsole(bool clean)
    {
        if (_busy)
        {
            Msg("Ortho already in progress…");
            return;
        }

        try
        {
            MelonCoroutines.Start(CoCaptureSingle(clean, DefaultResolution));
        }
        catch (Exception ex)
        {
            MelonLogger.Error($"dump_ortho failed: {ex}");
            try { Il2Cpp.uConsoleLog.Add($"TerrainDumper ERROR: {ex.Message}"); } catch { /* ignore */ }
        }
    }

    public static void RunTiledFromConsole(int grid = DefaultTileGrid, int tileRes = DefaultTileRes)
    {
        StartTiled(grid, tileRes, landMode: false, commandName: "dump_ortho_tiled");
    }

    /// <summary>
    /// Problematic multi-terrain regions (e.g. Cannery/Bleak Inlet): land-only bounds,
    /// suspend water/ice sheets, pre-look land tiles, gameplay-cam fallback for clear tiles.
    /// Writes ortho_color_tiled_land.png (does not overwrite classic tiled).
    /// </summary>
    public static void RunTiledLandFromConsole(int grid = DefaultTileGrid, int tileRes = DefaultTileRes)
    {
        StartTiled(grid, tileRes, landMode: true, commandName: "dump_ortho_tiled_land");
    }

    private static void StartTiled(int grid, int tileRes, bool landMode, string commandName)
    {
        if (_busy)
        {
            Msg("Ortho already in progress…");
            return;
        }

        if (grid < MinTileGrid || grid > MaxTileGrid)
        {
            Msg($"{commandName}: grid must be {MinTileGrid}..{MaxTileGrid} (got {grid}).");
            return;
        }

        if (tileRes < MinTileRes || tileRes > MaxTileRes)
        {
            Msg($"{commandName}: tileRes must be {MinTileRes}..{MaxTileRes} (got {tileRes}). Usage: {commandName} [grid] [tileRes]");
            return;
        }

        try
        {
            MelonCoroutines.Start(CoCaptureTiled(grid, tileRes, landMode));
        }
        catch (Exception ex)
        {
            MelonLogger.Error($"{commandName} failed: {ex}");
            _busy = false;
            try { Il2Cpp.uConsoleLog.Add($"TerrainDumper ERROR: {ex.Message}"); } catch { /* ignore */ }
        }
    }

    private static void BeginBusy()
    {
        _busy = true;
        DumpBackground.Acquire();
    }

    private static void EndBusy()
    {
        if (!_busy)
            return;
        _busy = false;
        DumpBackground.Release();
    }

    /// <summary>UniStorm fog scale saved before ortho; restored in RestoreQuality when suspended.</summary>
    private static float? _savedUniStormFogScale;
    private static bool _uniStormFogSuspended;

    /// <summary>
    /// Clear weather / calm wind / noon / zero fog via vanilla console so ortho colour is consistent.
    /// See https://the-long-dark-modding.fandom.com/wiki/Console_commands
    /// fog_scale 0 kills UniStorm height fog (CrashMountain blue wash).
    /// </summary>
    private static IEnumerator CoPrepareLighting()
    {
        Msg("Ortho lighting: lock_weather_instant clear, set_wind_immediate calm n, set_time 12, fog_scale 0…");
        bool any = false;
        any |= TryRunConsole("lock_weather_instant clear", "could not clear weather");
        any |= TryRunConsole("set_wind_immediate calm n", "could not calm wind");
        any |= TryRunConsole("set_time 12", "could not force noon");
        any |= SuspendUniStormFogScale();
        if (any)
        {
            for (int i = 0; i < 60; i++)
                yield return null;
        }
        else
        {
            Msg("WARNING: weather/noon/fog console commands failed — ortho may use current lighting");
        }
    }

    /// <summary>
    /// Save UniStorm fog scale, then force fog_scale 0 (console + direct field).
    /// </summary>
    private static bool SuspendUniStormFogScale()
    {
        _savedUniStormFogScale = null;
        _uniStormFogSuspended = false;
        try
        {
            // Il2Cpp interop exposes m_FogScale as a static property on the type.
            _savedUniStormFogScale = Il2Cpp.UniStormWeatherSystem.m_FogScale;
        }
        catch (Exception ex)
        {
            MelonLogger.Warning($"Ortho: could not read UniStorm fog scale: {ex.Message}");
        }

        bool ok = TryRunConsole("fog_scale 0", "could not zero fog_scale");
        try
        {
            Il2Cpp.UniStormWeatherSystem.m_FogScale = 0f;
            ok = true;
        }
        catch (Exception ex)
        {
            MelonLogger.Warning($"Ortho: could not set UniStorm fog scale: {ex.Message}");
        }

        _uniStormFogSuspended = ok || _savedUniStormFogScale.HasValue;
        return ok;
    }

    private static void RestoreUniStormFogScale()
    {
        if (!_uniStormFogSuspended)
            return;
        _uniStormFogSuspended = false;

        float scale = _savedUniStormFogScale ?? 1f;
        _savedUniStormFogScale = null;
        try
        {
            Il2Cpp.UniStormWeatherSystem.m_FogScale = scale;
        }
        catch (Exception ex)
        {
            MelonLogger.Warning($"Ortho: could not restore UniStorm fog scale: {ex.Message}");
        }

        TryRunConsole(
            "fog_scale " + scale.ToString("G", CultureInfo.InvariantCulture),
            "could not restore fog_scale");
    }

    private static bool TryRunConsole(string command, string failHint)
    {
        try
        {
            Il2Cpp.uConsole.RunCommand(command);
            return true;
        }
        catch (Exception ex)
        {
            MelonLogger.Warning($"Ortho '{command}' failed ({failHint}): {ex.Message}");
            return false;
        }
    }

    private static IEnumerator CoCaptureSingle(bool clean, int resolution)
    {
        BeginBusy();
        List<TerrainLodRestore>? lodRestore = null;
        bool fogWas = RenderSettings.fog;
        float shadowDist = QualitySettings.shadowDistance;
        int pixelLight = QualitySettings.pixelLightCount;
        float lodBias = QualitySettings.lodBias;

        GameObject? camGo = null;
        RenderTexture? rt = null;
        Texture2D? tex = null;
        Exception? failure = null;

        try
        {
            yield return CoPrepareLighting();

            ComputeBounds(out float minX, out float maxX, out float minZ, out float maxZ, out float minY, out float maxY, landOnly: false);
            float extent = Math.Max(maxX - minX, maxZ - minZ);
            float cx = (minX + maxX) * 0.5f;
            float cz = (minZ + maxZ) * 0.5f;
            float camHeight = maxY + Math.Max(500f, extent);

            string sceneName = UnitySceneManager.GetActiveScene().name;
            if (string.IsNullOrWhiteSpace(sceneName))
                sceneName = "UnknownScene";
            string outDir = Path.Combine(MelonEnvironment.ModsDirectory, "TerrainDumper", sceneName);
            Directory.CreateDirectory(outDir);

            if (clean)
                lodRestore = ApplyHiLod(keepTrees: false);
            else
                lodRestore = ApplyHiLod(keepTrees: true);

            RenderSettings.fog = false;
            QualitySettings.shadowDistance = Math.Max(shadowDist, 2000f);
            QualitySettings.pixelLightCount = Math.Max(pixelLight, 4);
            QualitySettings.lodBias = Math.Max(lodBias, 4f);

            // Let LOD/fog settle one frame before Render.
            yield return null;

            try
            {
                camGo = CreateOrthoCam();
                Camera cam = camGo.GetComponent<Camera>();
                PlaceOrthoCam(camGo, cam, cx, camHeight, cz, extent * 0.5f, camHeight - minY + 200f);

                rt = new RenderTexture(resolution, resolution, 24, RenderTextureFormat.ARGB32);
                rt.antiAliasing = 1;
                cam.targetTexture = rt;
                cam.Render();

                tex = ReadRt(rt, resolution);

                string stem = clean ? "ortho_color_clean" : (ForceVisible.IsActive ? "ortho_color_forcevis" : "ortho_color");
                string pngPath = Path.Combine(outDir, stem + ".png");
                File.WriteAllBytes(pngPath, ImageConversion.EncodeToPNG(tex));

                WriteMeta(new OrthoMeta
                {
                    FormatVersion = 4,
                    SceneName = sceneName,
                    DumpedAtUtc = DateTime.UtcNow.ToString("o", CultureInfo.InvariantCulture),
                    ModVersion = Implementation.ModVersion,
                    Clean = clean,
                    TempCamera = true,
                    ForceVisible = ForceVisible.IsActive ? true : null,
                    Resolution = resolution,
                    FileName = stem + ".png",
                    CenterX = cx,
                    CenterZ = cz,
                    OrthographicSize = extent * 0.5f,
                    CameraHeight = camHeight,
                    BoundsMinX = minX,
                    BoundsMaxX = maxX,
                    BoundsMinZ = minZ,
                    BoundsMaxZ = maxZ,
                    BoundsMinY = minY,
                    BoundsMaxY = maxY,
                    Notes = new[]
                    {
                        "Dedicated TerrainDumper_OrthoCam + RenderTexture (not gameplay camera).",
                        clean ? "Clean: trees/details off." : "Default: hi-LOD trees kept where terrain draws them.",
                        ForceVisible.IsActive ? "ForceVisible was ON during this capture." : "ForceVisible off.",
                        "UniStorm fog_scale forced to 0 for capture (restored after).",
                    },
                }, Path.Combine(outDir, stem + "_meta.json"));

                Msg($"Ortho {(clean ? "clean" : "default")} wrote {pngPath}");
            }
            catch (Exception ex)
            {
                failure = ex;
            }
        }
        finally
        {
            CleanupCapture(camGo, rt, tex);
            RestoreQuality(fogWas, shadowDist, pixelLight, lodBias, lodRestore);
            EndBusy();
        }

        if (failure != null)
            MelonLogger.Error($"dump_ortho failed: {failure}");
    }

    private static IEnumerator CoCaptureTiled(int grid, int tileRes, bool landMode)
    {
        BeginBusy();
        List<TerrainLodRestore>? lodRestore = null;
        List<BackdropRestore>? backdropRestore = null;
        bool fogWas = RenderSettings.fog;
        float shadowDist = QualitySettings.shadowDistance;
        int pixelLight = QualitySettings.pixelLightCount;
        float lodBias = QualitySettings.lodBias;

        GameObject? camGo = null;
        RenderTexture? rt = null;
        Texture2D? atlas = null;

        try
        {
            yield return CoPrepareLighting();

            // Full land+water AABB first (water must still be active to appear in activeTerrains).
            ComputeBounds(
                out float minX, out float maxX, out float minZ, out float maxZ, out float minY, out float maxY,
                landOnly: false);

            if (landMode)
                backdropRestore = SuspendBackdropTerrains();

            float extent = Math.Max(maxX - minX, maxZ - minZ);
            float cx = (minX + maxX) * 0.5f;
            float cz = (minZ + maxZ) * 0.5f;
            float half = extent * 0.5f;
            float tileWorld = extent / grid;
            float squareMinX = cx - half;
            float squareMinZ = cz - half;

            string sceneName = UnitySceneManager.GetActiveScene().name;
            if (string.IsNullOrWhiteSpace(sceneName))
                sceneName = "UnknownScene";
            string outDir = Path.Combine(MelonEnvironment.ModsDirectory, "TerrainDumper", sceneName);
            Directory.CreateDirectory(outDir);

            lodRestore = ApplyHiLod(keepTrees: true);
            RenderSettings.fog = false;
            QualitySettings.shadowDistance = Math.Max(shadowDist, 4000f);
            QualitySettings.pixelLightCount = Math.Max(pixelLight, 4);
            QualitySettings.lodBias = Math.Max(lodBias, 8f);

            yield return null;

            camGo = CreateOrthoCam();
            Camera cam = camGo.GetComponent<Camera>();
            rt = new RenderTexture(tileRes, tileRes, 24, RenderTextureFormat.ARGB32);
            rt.antiAliasing = 1;
            cam.targetTexture = rt;

            if (landMode)
            {
                TrySetKeepUnusedCameraResources(cam, keep: true);
                try
                {
                    Transform? tr = Il2Cpp.GameManager.GetPlayerTransform();
                    if (tr != null)
                    {
                        _savedPlayerPos = tr.position;
                        _savedPlayerRot = tr.rotation;
                    }
                }
                catch (Exception ex)
                {
                    MelonLogger.Warning($"Ortho land-mode: could not save player pose: {ex.Message}");
                }
            }

            int atlasRes = grid * tileRes;
            atlas = new Texture2D(atlasRes, atlasRes, TextureFormat.RGB24, false);
            string modeLabel = landMode ? "land-mode" : "classic";
            Msg($"Ortho tiled ({modeLabel}): {grid}x{grid} @ {tileRes}px → {atlasRes}px atlas…");

            float camHeight = maxY + Math.Max(120f, tileWorld * 1.25f);
            float orthoSize = tileWorld * 0.5f;
            float farClip = camHeight - minY + 200f;
            int playerNudges = 0;
            int waterCells = 0;
            int overlapCells = 0;

            if (landMode)
            {
                // Pass 1: interior land only — skip cells whose footprint hits water AABB
                // (use suspended backdrop list; disabled terrains are not in activeTerrains).
                bool[,] filled = new bool[grid, grid];
                Terrain[] terrains = Terrain.activeTerrains;
                if (terrains != null)
                {
                    foreach (Terrain land in terrains)
                    {
                        if (land == null || land.terrainData == null || !land.enabled || IsBackdropTerrain(land))
                            continue;

                        bool ok = false;
                        yield return CoTeleportPlayerToTerrain(land, success => ok = success);
                        if (!ok)
                            continue;
                        playerNudges++;

                        int captured = 0;
                        for (int tz = 0; tz < grid; tz++)
                        {
                            for (int tx = 0; tx < grid; tx++)
                            {
                                if (filled[tx, tz])
                                    continue;
                                float tcx = squareMinX + (tx + 0.5f) * tileWorld;
                                float tcz = squareMinZ + (tz + 0.5f) * tileWorld;
                                if (!WorldPointOnTerrain(tcx, tcz, land))
                                    continue;
                                if (AtlasCellIntersectsBackdropList(
                                        squareMinX, squareMinZ, tileWorld, tx, tz, backdropRestore))
                                    continue;

                                yield return CoCaptureAtlasCell(
                                    camGo, cam, rt, atlas, tx, tz, tileRes,
                                    tcx, camHeight, tcz, orthoSize, farClip,
                                    retryIfClear: true);
                                filled[tx, tz] = true;
                                captured++;
                            }
                        }

                        MelonLogger.Msg(
                            $"Ortho land-mode: {land.gameObject.name} → {captured} atlas cell(s)");
                    }
                }

                // Pass 2: restore water/ice so lakes/coastal ice can draw.
                if (backdropRestore != null && backdropRestore.Count > 0)
                {
                    RestoreBackdropTerrains(backdropRestore);
                    foreach (BackdropRestore br in backdropRestore)
                    {
                        if (br.Terrain == null || br.Terrain.terrainData == null)
                            continue;
                        try
                        {
                            br.Terrain.basemapDistance = 50000f;
                            br.Terrain.heightmapPixelError = 1f;
                            br.Terrain.heightmapMaximumLOD = 0;
                        }
                        catch { /* ignore */ }
                    }
                    Msg($"Ortho land-mode: restored {backdropRestore.Count} water/ice terrain(s)…");
                    for (int i = 0; i < 45; i++)
                        yield return null;
                }

                // Pass 3: overlap — any atlas cell whose footprint hits land∩water (ice shelves).
                terrains = Terrain.activeTerrains;
                if (terrains != null)
                {
                    foreach (Terrain land in terrains)
                    {
                        if (land == null || land.terrainData == null || !land.enabled || IsBackdropTerrain(land))
                            continue;

                        int need = 0;
                        for (int tz = 0; tz < grid; tz++)
                        {
                            for (int tx = 0; tx < grid; tx++)
                            {
                                if (!AtlasCellIntersectsTerrain(squareMinX, squareMinZ, tileWorld, tx, tz, land))
                                    continue;
                                if (!AtlasCellIntersectsAnyBackdrop(squareMinX, squareMinZ, tileWorld, tx, tz))
                                    continue;
                                need++;
                            }
                        }
                        if (need == 0)
                            continue;

                        bool ok = false;
                        yield return CoTeleportPlayerToTerrain(land, success => ok = success);
                        if (!ok)
                            continue;
                        playerNudges++;

                        int recaptured = 0;
                        for (int tz = 0; tz < grid; tz++)
                        {
                            for (int tx = 0; tx < grid; tx++)
                            {
                                if (!AtlasCellIntersectsTerrain(squareMinX, squareMinZ, tileWorld, tx, tz, land))
                                    continue;
                                if (!AtlasCellIntersectsAnyBackdrop(squareMinX, squareMinZ, tileWorld, tx, tz))
                                    continue;

                                float tcx = squareMinX + (tx + 0.5f) * tileWorld;
                                float tcz = squareMinZ + (tz + 0.5f) * tileWorld;
                                yield return CoCaptureAtlasCell(
                                    camGo, cam, rt, atlas, tx, tz, tileRes,
                                    tcx, camHeight, tcz, orthoSize, farClip,
                                    retryIfClear: false);
                                filled[tx, tz] = true;
                                recaptured++;
                                overlapCells++;
                            }
                        }

                        MelonLogger.Msg(
                            $"Ortho land-mode overlap: {land.gameObject.name} → {recaptured} cell(s)");
                    }
                }

                // Pass 4: water-only / void cells never claimed by land.
                for (int tz = 0; tz < grid; tz++)
                {
                    for (int tx = 0; tx < grid; tx++)
                    {
                        if (filled[tx, tz])
                            continue;
                        float tcx = squareMinX + (tx + 0.5f) * tileWorld;
                        float tcz = squareMinZ + (tz + 0.5f) * tileWorld;
                        yield return CoCaptureAtlasCell(
                            camGo, cam, rt, atlas, tx, tz, tileRes,
                            tcx, camHeight, tcz, orthoSize, farClip,
                            retryIfClear: false);
                        filled[tx, tz] = true;
                        waterCells++;
                    }
                }

                if (overlapCells > 0 || waterCells > 0)
                {
                    Msg(
                        $"Ortho land-mode: overlap recapture {overlapCells} cell(s), " +
                        $"water/void fill {waterCells} cell(s)");
                }

                if (_savedPlayerPos.HasValue)
                {
                    yield return CoTeleportPlayerTo(_savedPlayerPos.Value, _savedPlayerRot);
                    _savedPlayerPos = null;
                }
            }
            else
            {
                for (int index = 0; index < grid * grid; index++)
                {
                    int tx = index % grid;
                    int tz = index / grid;
                    float tcx = squareMinX + (tx + 0.5f) * tileWorld;
                    float tcz = squareMinZ + (tz + 0.5f) * tileWorld;
                    yield return CoCaptureAtlasCell(
                        camGo, cam, rt, atlas, tx, tz, tileRes,
                        tcx, camHeight, tcz, orthoSize, farClip,
                        retryIfClear: false);

                    if ((index + 1) % grid == 0 || index + 1 == grid * grid)
                        MelonLogger.Msg($"Ortho tiled {index + 1}/{grid * grid}…");
                }
            }

            atlas.Apply(false, false);

            string stem = landMode ? "ortho_color_tiled_land" : "ortho_color_tiled";
            string pngPath = Path.Combine(outDir, stem + ".png");
            File.WriteAllBytes(pngPath, ImageConversion.EncodeToPNG(atlas));

            var notes = new List<string>
            {
                "Tiled ortho via dedicated TerrainDumper_OrthoCam + RenderTexture (not gameplay camera / ScreenCapture).",
                "Same world square (center + orthographicSize) for warp_ortho_to_image.",
                $"Grid {grid}x{grid}, tile {tileRes}px, atlas {atlasRes}px.",
                "UniStorm fog_scale forced to 0 for capture (restored after).",
            };
            if (landMode)
            {
                notes.Add("LAND MODE: interior land (water off) → footprint-overlap shore (land+water) → water/void fill.");
                notes.Add(
                    $"Player nudges: {playerNudges}; overlap cells: {overlapCells}; water/void cells: {waterCells}.");
            }

            WriteMeta(new OrthoMeta
            {
                FormatVersion = 4,
                SceneName = sceneName,
                DumpedAtUtc = DateTime.UtcNow.ToString("o", CultureInfo.InvariantCulture),
                ModVersion = Implementation.ModVersion,
                Clean = false,
                Tiled = true,
                HiLod = true,
                TempCamera = true,
                ForceVisible = ForceVisible.IsActive ? true : null,
                LandOnlyBounds = landMode ? false : null,
                SuspendedBackdrops = landMode ? backdropRestore?.Count : null,
                PlayerNudges = landMode ? playerNudges : null,
                OverlapCells = landMode ? overlapCells : null,
                WaterCells = landMode ? waterCells : null,
                TileGrid = grid,
                TileResolution = tileRes,
                Resolution = atlasRes,
                FileName = stem + ".png",
                CenterX = cx,
                CenterZ = cz,
                OrthographicSize = half,
                CameraHeight = maxY + Math.Max(120f, tileWorld * 1.25f),
                BoundsMinX = minX,
                BoundsMaxX = maxX,
                BoundsMinZ = minZ,
                BoundsMaxZ = maxZ,
                BoundsMinY = minY,
                BoundsMaxY = maxY,
                Notes = notes.ToArray(),
            }, Path.Combine(outDir, stem + "_meta.json"));

            Msg($"Ortho tiled done → {pngPath}");
        }
        finally
        {
            // Best-effort restore if capture aborted mid-nudge.
            if (_savedPlayerPos.HasValue)
            {
                try
                {
                    var pm = Il2Cpp.GameManager.GetPlayerManagerComponent();
                    if (pm != null)
                        pm.TeleportPlayer(_savedPlayerPos.Value, _savedPlayerRot);
                }
                catch { /* ignore */ }
                _savedPlayerPos = null;
            }
            if (camGo != null)
            {
                try
                {
                    Camera c = camGo.GetComponent<Camera>();
                    if (c != null)
                        TrySetKeepUnusedCameraResources(c, keep: false);
                }
                catch { /* ignore */ }
            }
            CleanupCapture(camGo, rt, atlas);
            RestoreQuality(fogWas, shadowDist, pixelLight, lodBias, lodRestore);
            RestoreBackdropTerrains(backdropRestore);
            EndBusy();
        }
    }

    private static Vector3? _savedPlayerPos;
    private static Quaternion _savedPlayerRot;

    private static IEnumerator CoCaptureAtlasCell(
        GameObject camGo, Camera cam, RenderTexture rt, Texture2D atlas,
        int tx, int tz, int tileRes,
        float tcx, float camHeight, float tcz, float orthoSize, float farClip,
        bool retryIfClear)
    {
        PlaceOrthoCam(camGo, cam, tcx, camHeight, tcz, orthoSize, farClip);
        cam.Render();
        Texture2D tile = ReadRt(rt, tileRes);

        if (retryIfClear && IsMostlyClearColor(tile))
        {
            MelonLogger.Msg($"Ortho land-mode: atlas {tx},{tz} clear — extra settle + retry…");
            UnityEngine.Object.Destroy(tile);
            for (int i = 0; i < 60; i++)
                yield return null;
            PlaceOrthoCam(camGo, cam, tcx, camHeight, tcz, orthoSize, farClip);
            cam.Render();
            tile = ReadRt(rt, tileRes);
        }

        atlas.SetPixels(tx * tileRes, tz * tileRes, tileRes, tileRes, tile.GetPixels());
        UnityEngine.Object.Destroy(tile);
        yield return null;
    }

    private static bool IsMostlyClearColor(Texture2D tex, float threshold = 0.55f)
    {
        if (tex == null)
            return true;
        Color[] px = tex.GetPixels();
        if (px == null || px.Length == 0)
            return true;

        const float cr = 0.12f, cg = 0.12f, cb = 0.14f;
        int hit = 0, n = 0;
        for (int i = 0; i < px.Length; i += 16)
        {
            Color c = px[i];
            n++;
            if (Mathf.Abs(c.r - cr) + Mathf.Abs(c.g - cg) + Mathf.Abs(c.b - cb) < 0.15f)
                hit++;
        }
        return n > 0 && (float)hit / n >= threshold;
    }

    private static bool AtlasCellIntersectsBackdropList(
        float squareMinX, float squareMinZ, float tileWorld, int tx, int tz,
        List<BackdropRestore>? backdrops)
    {
        if (backdrops == null)
            return AtlasCellIntersectsAnyBackdrop(squareMinX, squareMinZ, tileWorld, tx, tz);
        foreach (BackdropRestore br in backdrops)
        {
            if (br.Terrain == null || br.Terrain.terrainData == null)
                continue;
            if (AtlasCellIntersectsTerrain(squareMinX, squareMinZ, tileWorld, tx, tz, br.Terrain))
                return true;
        }
        return false;
    }

    private static bool AtlasCellIntersectsAnyBackdrop(
        float squareMinX, float squareMinZ, float tileWorld, int tx, int tz)
    {
        Terrain[] terrains = Terrain.activeTerrains;
        if (terrains == null)
            return false;
        foreach (Terrain t in terrains)
        {
            if (t == null || t.terrainData == null || !t.enabled || !IsBackdropTerrain(t))
                continue;
            if (AtlasCellIntersectsTerrain(squareMinX, squareMinZ, tileWorld, tx, tz, t))
                return true;
        }
        return false;
    }

    private static bool AtlasCellIntersectsTerrain(
        float squareMinX, float squareMinZ, float tileWorld, int tx, int tz, Terrain t)
    {
        if (t == null || t.terrainData == null)
            return false;
        float cellMinX = squareMinX + tx * tileWorld;
        float cellMinZ = squareMinZ + tz * tileWorld;
        float cellMaxX = cellMinX + tileWorld;
        float cellMaxZ = cellMinZ + tileWorld;
        Vector3 pos = t.transform.position;
        Vector3 size = t.terrainData.size;
        float tMinX = pos.x, tMinZ = pos.z;
        float tMaxX = pos.x + size.x, tMaxZ = pos.z + size.z;
        return cellMinX < tMaxX && cellMaxX > tMinX && cellMinZ < tMaxZ && cellMaxZ > tMinZ;
    }

    private static bool WorldPointOnAnyBackdrop(float wx, float wz)
    {
        Terrain[] terrains = Terrain.activeTerrains;
        if (terrains == null)
            return false;
        foreach (Terrain t in terrains)
        {
            if (t == null || t.terrainData == null || !t.enabled || !IsBackdropTerrain(t))
                continue;
            if (WorldPointOnTerrain(wx, wz, t))
                return true;
        }
        return false;
    }

    private static bool WorldPointOnTerrain(float wx, float wz, Terrain t)
    {
        if (t == null || t.terrainData == null)
            return false;
        Vector3 pos = t.transform.position;
        Vector3 size = t.terrainData.size;
        // Half-open on max so shared edges belong to one tile.
        return wx >= pos.x && wx < pos.x + size.x && wz >= pos.z && wz < pos.z + size.z;
    }

    private static Terrain? FindLandTerrainAt(float wx, float wz)
    {
        Terrain[] terrains = Terrain.activeTerrains;
        if (terrains == null)
            return null;
        foreach (Terrain t in terrains)
        {
            if (t == null || t.terrainData == null || !t.enabled || IsBackdropTerrain(t))
                continue;
            if (WorldPointOnTerrain(wx, wz, t))
                return t;
        }
        return null;
    }

    private static IEnumerator CoTeleportPlayerToTerrain(Terrain t, Action<bool> onDone)
    {
        if (t == null || t.terrainData == null)
        {
            onDone(false);
            yield break;
        }

        Vector3 pos = t.transform.position;
        Vector3 size = t.terrainData.size;
        float tcx = pos.x + size.x * 0.5f;
        float tcz = pos.z + size.z * 0.5f;
        float y = _savedPlayerPos?.y ?? pos.y + 5f;
        try
        {
            y = t.SampleHeight(new Vector3(tcx, 0f, tcz)) + 3f;
        }
        catch { /* keep fallback y */ }

        string name = t.gameObject != null ? t.gameObject.name : t.name;
        MelonLogger.Msg($"Ortho land-mode: player → {name} ({tcx:0},{tcz:0})");
        yield return CoTeleportPlayerTo(new Vector3(tcx, y, tcz), _savedPlayerRot);
        onDone(true);
    }

    private static IEnumerator CoTeleportPlayerTo(Vector3 pos, Quaternion rot)
    {
        Il2Cpp.PlayerManager? pm = null;
        try { pm = Il2Cpp.GameManager.GetPlayerManagerComponent(); }
        catch (Exception ex)
        {
            MelonLogger.Warning($"Ortho land-mode TeleportPlayer get PM failed: {ex.Message}");
            yield break;
        }

        if (pm == null)
            yield break;

        try
        {
            pm.TeleportPlayer(pos, rot);
        }
        catch (Exception ex)
        {
            MelonLogger.Warning($"Ortho land-mode TeleportPlayer failed: {ex.Message}");
            yield break;
        }

        // Let scene streaming / terrain splat catch up near the new pose.
        for (int i = 0; i < 90; i++)
            yield return null;
    }

    private static GameObject CreateOrthoCam()
    {
        var go = new GameObject("TerrainDumper_OrthoCam");
        Camera cam = go.AddComponent<Camera>();
        cam.orthographic = true;
        cam.aspect = 1f;
        cam.clearFlags = CameraClearFlags.SolidColor;
        cam.backgroundColor = new Color(0.12f, 0.12f, 0.14f, 1f);
        cam.allowHDR = false;
        cam.allowMSAA = false;
        cam.cullingMask = ~0;
        cam.useOcclusionCulling = false;
        cam.enabled = false;
        cam.targetTexture = null;
        return go;
    }

    private static void PlaceOrthoCam(
        GameObject go, Camera cam, float x, float y, float z, float orthoSize, float farClip)
    {
        go.transform.position = new Vector3(x, y, z);
        go.transform.rotation = Quaternion.Euler(90f, 0f, 0f);
        cam.orthographic = true;
        cam.orthographicSize = orthoSize;
        cam.aspect = 1f;
        cam.nearClipPlane = 1f;
        cam.farClipPlane = Math.Max(farClip, 100f);
    }

    private static Texture2D ReadRt(RenderTexture rt, int resolution)
    {
        RenderTexture prev = RenderTexture.active;
        RenderTexture.active = rt;
        var tex = new Texture2D(resolution, resolution, TextureFormat.RGB24, false);
        tex.ReadPixels(new Rect(0, 0, resolution, resolution), 0, 0);
        tex.Apply(false, false);
        RenderTexture.active = prev;
        return tex;
    }

    private static void CleanupCapture(GameObject? camGo, RenderTexture? rt, Texture2D? tex)
    {
        if (tex != null)
            UnityEngine.Object.Destroy(tex);
        if (camGo != null)
        {
            Camera cam = camGo.GetComponent<Camera>();
            if (cam != null)
                cam.targetTexture = null;
            UnityEngine.Object.Destroy(camGo);
        }
        if (rt != null)
        {
            rt.Release();
            UnityEngine.Object.Destroy(rt);
        }
    }

    private static void WriteMeta(OrthoMeta meta, string path)
    {
        File.WriteAllText(
            path,
            JsonSerializer.Serialize(meta, JsonOpts),
            new UTF8Encoding(encoderShouldEmitUTF8Identifier: false));
    }

    /// <summary>
    /// Point OrthoCam at each land Terrain once so Unity allocates render/splat resources
    /// before the atlas pass (distant columns otherwise stay clear-color void).
    /// </summary>
    private static IEnumerator CoPreloadLandTerrains(
        GameObject camGo, Camera cam, float minY, float maxY, Action<int> onDone)
    {
        Terrain[] terrains = Terrain.activeTerrains;
        int n = 0;
        if (terrains != null)
        {
            foreach (Terrain t in terrains)
            {
                if (t == null || t.terrainData == null || IsBackdropTerrain(t))
                    continue;
                try
                {
                    TrySetKeepUnusedOnTerrain(t, cam, keep: true);
                    Vector3 pos = t.transform.position;
                    Vector3 size = t.terrainData.size;
                    float tcx = pos.x + size.x * 0.5f;
                    float tcz = pos.z + size.z * 0.5f;
                    float half = Math.Max(size.x, size.z) * 0.5f;
                    float camHeight = maxY + Math.Max(120f, half * 2.5f);
                    PlaceOrthoCam(camGo, cam, tcx, camHeight, tcz, half, camHeight - minY + 200f);
                    cam.Render();
                    n++;
                }
                catch (Exception ex)
                {
                    MelonLogger.Warning($"Ortho land pre-look failed on {t.name}: {ex.Message}");
                }

                yield return null;
            }
        }

        if (n > 0)
            Msg($"Ortho: pre-looked {n} land terrain(s)");
        onDone?.Invoke(n);
    }

    private static void TrySetKeepUnusedCameraResources(Camera cam, bool keep)
    {
        if (cam == null)
            return;
        Terrain[] terrains = Terrain.activeTerrains;
        if (terrains == null)
            return;
        foreach (Terrain t in terrains)
        {
            if (t == null || IsBackdropTerrain(t))
                continue;
            TrySetKeepUnusedOnTerrain(t, cam, keep);
        }
    }

    private static void TrySetKeepUnusedOnTerrain(Terrain t, Camera cam, bool keep)
    {
        if (t == null || cam == null)
            return;
        try
        {
            // Il2Cpp binds this as an instance method (Unity stock API is static).
            t.SetKeepUnusedCameraRenderingResources(cam.GetInstanceID(), keep);
        }
        catch (Exception ex)
        {
            MelonLogger.Warning($"Ortho KeepUnused on {t.name} failed: {ex.Message}");
        }
    }

    /// <summary>
    /// Water/ice sheets that should not widen ortho AABB and should not draw over land splats.
    /// Matches mapalign._is_backdrop_tile name tokens.
    /// </summary>
    private static bool IsBackdropTerrain(Terrain t)
    {
        if (t == null)
            return true;
        string name = "";
        try
        {
            if (t.gameObject != null)
                name = t.gameObject.name ?? "";
        }
        catch
        {
            /* ignore */
        }

        name = name.ToLowerInvariant();
        return name.Contains("water")
               || name.Contains("ice")
               || name.Contains("pond")
               || name.Contains("creek");
    }

    private static List<BackdropRestore> SuspendBackdropTerrains()
    {
        var list = new List<BackdropRestore>();
        Terrain[] terrains = Terrain.activeTerrains;
        if (terrains == null)
            return list;

        foreach (Terrain t in terrains)
        {
            if (t == null || !IsBackdropTerrain(t))
                continue;
            list.Add(new BackdropRestore(t, t.enabled));
            t.enabled = false;
        }

        if (list.Count > 0)
            Msg($"Ortho: suspended {list.Count} water/ice terrain(s) for capture");
        return list;
    }

    private static void RestoreBackdropTerrains(List<BackdropRestore>? list)
    {
        if (list == null)
            return;
        foreach (BackdropRestore r in list)
        {
            if (r.Terrain == null)
                continue;
            try { r.Terrain.enabled = r.WasEnabled; }
            catch (Exception ex)
            {
                MelonLogger.Warning($"Ortho: failed restoring backdrop terrain: {ex.Message}");
            }
        }
    }

    private static List<TerrainLodRestore> ApplyHiLod(bool keepTrees)
    {
        var list = new List<TerrainLodRestore>();
        Terrain[] terrains = Terrain.activeTerrains;
        if (terrains == null)
            return list;

        foreach (Terrain t in terrains)
        {
            if (t == null || t.terrainData == null || !t.enabled)
                continue;
            list.Add(new TerrainLodRestore(
                t, t.drawTreesAndFoliage, t.treeDistance, t.detailObjectDistance,
                t.heightmapPixelError, t.basemapDistance, t.heightmapMaximumLOD));
            t.heightmapPixelError = 1f;
            t.basemapDistance = 50000f;
            t.heightmapMaximumLOD = 0;
            if (keepTrees)
            {
                t.drawTreesAndFoliage = true;
                t.treeDistance = 50000f;
                t.detailObjectDistance = 500f;
            }
            else
            {
                t.drawTreesAndFoliage = false;
                t.treeDistance = 0f;
                t.detailObjectDistance = 0f;
            }
        }
        return list;
    }

    private static void RestoreQuality(
        bool fogWas, float shadowDist, int pixelLight, float lodBias, List<TerrainLodRestore>? lodRestore)
    {
        RenderSettings.fog = fogWas;
        QualitySettings.shadowDistance = shadowDist;
        QualitySettings.pixelLightCount = pixelLight;
        QualitySettings.lodBias = lodBias;
        RestoreUniStormFogScale();
        if (lodRestore == null)
            return;
        foreach (var r in lodRestore)
        {
            if (r.Terrain == null)
                continue;
            r.Terrain.drawTreesAndFoliage = r.DrawTrees;
            r.Terrain.treeDistance = r.TreeDist;
            r.Terrain.detailObjectDistance = r.DetailDist;
            r.Terrain.heightmapPixelError = r.HeightError;
            r.Terrain.basemapDistance = r.BasemapDist;
            r.Terrain.heightmapMaximumLOD = r.HeightmapMaxLod;
        }
    }

    private static void ComputeBounds(
        out float minX, out float maxX, out float minZ, out float maxZ, out float minY, out float maxY,
        bool landOnly = false)
    {
        Terrain[] terrains = Terrain.activeTerrains;
        if (terrains == null || terrains.Length == 0)
            throw new InvalidOperationException("No active Terrains — load a region first.");

        if (!TryAccumulateBounds(terrains, landOnly, out minX, out maxX, out minZ, out maxZ, out minY, out maxY))
        {
            if (landOnly)
            {
                MelonLogger.Warning("Ortho: no land Terrains for bounds; falling back to all active Terrains.");
                if (TryAccumulateBounds(terrains, landOnly: false, out minX, out maxX, out minZ, out maxZ, out minY, out maxY))
                    return;
            }
            throw new InvalidOperationException("Could not compute terrain bounds (no usable active Terrains).");
        }
    }

    private static bool TryAccumulateBounds(
        Terrain[] terrains,
        bool landOnly,
        out float minX, out float maxX, out float minZ, out float maxZ, out float minY, out float maxY)
    {
        minX = float.PositiveInfinity;
        maxX = float.NegativeInfinity;
        minZ = float.PositiveInfinity;
        maxZ = float.NegativeInfinity;
        minY = float.PositiveInfinity;
        maxY = float.NegativeInfinity;
        int used = 0;

        foreach (Terrain t in terrains)
        {
            if (t == null || t.terrainData == null)
                continue;
            if (landOnly && IsBackdropTerrain(t))
                continue;
            Vector3 pos = t.transform.position;
            // Unity Terrain ignores its Transform scale; terrainData.size is the world extent.
            Vector3 size = t.terrainData.size;
            float x0 = pos.x, z0 = pos.z;
            float x1 = pos.x + size.x, z1 = pos.z + size.z;
            float y0 = pos.y, y1 = pos.y + size.y;
            if (x0 < minX) minX = x0;
            if (x1 > maxX) maxX = x1;
            if (z0 < minZ) minZ = z0;
            if (z1 > maxZ) maxZ = z1;
            if (y0 < minY) minY = y0;
            if (y1 > maxY) maxY = y1;
            used++;
        }

        return used > 0 && float.IsFinite(minX);
    }

    private static void Msg(string text)
    {
        MelonLogger.Msg(text);
        try { Il2Cpp.uConsoleLog.Add("TerrainDumper: " + text); } catch { /* ignore */ }
    }

    private static readonly JsonSerializerOptions JsonOpts = new()
    {
        WriteIndented = true,
        DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull,
    };

    private readonly struct BackdropRestore
    {
        public readonly Terrain Terrain;
        public readonly bool WasEnabled;

        public BackdropRestore(Terrain t, bool wasEnabled)
        {
            Terrain = t;
            WasEnabled = wasEnabled;
        }
    }

    private readonly struct TerrainLodRestore
    {
        public readonly Terrain Terrain;
        public readonly bool DrawTrees;
        public readonly float TreeDist;
        public readonly float DetailDist;
        public readonly float HeightError;
        public readonly float BasemapDist;
        public readonly int HeightmapMaxLod;

        public TerrainLodRestore(
            Terrain t, bool draw, float tree, float detail, float heightErr, float basemap, int heightmapMaxLod)
        {
            Terrain = t;
            DrawTrees = draw;
            TreeDist = tree;
            DetailDist = detail;
            HeightError = heightErr;
            BasemapDist = basemap;
            HeightmapMaxLod = heightmapMaxLod;
        }
    }

    private sealed class OrthoMeta
    {
        [JsonPropertyName("formatVersion")] public int FormatVersion { get; set; }
        [JsonPropertyName("sceneName")] public string SceneName { get; set; } = "";
        [JsonPropertyName("dumpedAtUtc")] public string DumpedAtUtc { get; set; } = "";
        [JsonPropertyName("modVersion")] public string ModVersion { get; set; } = "";
        [JsonPropertyName("clean")] public bool Clean { get; set; }
        [JsonPropertyName("tiled")] public bool? Tiled { get; set; }
        [JsonPropertyName("hiLod")] public bool? HiLod { get; set; }
        [JsonPropertyName("tempCamera")] public bool? TempCamera { get; set; }
        [JsonPropertyName("forceVisible")] public bool? ForceVisible { get; set; }
        [JsonPropertyName("landOnlyBounds")] public bool? LandOnlyBounds { get; set; }
        [JsonPropertyName("suspendedBackdrops")] public int? SuspendedBackdrops { get; set; }
        [JsonPropertyName("landPreload")] public int? LandPreload { get; set; }
        [JsonPropertyName("playerNudges")] public int? PlayerNudges { get; set; }
        [JsonPropertyName("overlapCells")] public int? OverlapCells { get; set; }
        [JsonPropertyName("waterCells")] public int? WaterCells { get; set; }
        [JsonPropertyName("tileGrid")] public int? TileGrid { get; set; }
        [JsonPropertyName("tileResolution")] public int? TileResolution { get; set; }
        [JsonPropertyName("resolution")] public int Resolution { get; set; }
        [JsonPropertyName("fileName")] public string FileName { get; set; } = "";
        [JsonPropertyName("centerX")] public float CenterX { get; set; }
        [JsonPropertyName("centerZ")] public float CenterZ { get; set; }
        [JsonPropertyName("orthographicSize")] public float OrthographicSize { get; set; }
        [JsonPropertyName("cameraHeight")] public float CameraHeight { get; set; }
        [JsonPropertyName("boundsMinX")] public float BoundsMinX { get; set; }
        [JsonPropertyName("boundsMaxX")] public float BoundsMaxX { get; set; }
        [JsonPropertyName("boundsMinZ")] public float BoundsMinZ { get; set; }
        [JsonPropertyName("boundsMaxZ")] public float BoundsMaxZ { get; set; }
        [JsonPropertyName("boundsMinY")] public float BoundsMinY { get; set; }
        [JsonPropertyName("boundsMaxY")] public float BoundsMaxY { get; set; }
        [JsonPropertyName("notes")] public string[]? Notes { get; set; }
    }
}
