using System.Collections;
using MelonLoader;
using MelonLoader.Utils;
using UnityEngine;
using UnitySceneManager = UnityEngine.SceneManagement.SceneManager;

namespace TerrainDumper;

/// <summary>
/// One-shot pipeline for ship map_bg inputs:
/// terrain (+ fog/alignment) → names @ 2m → enrichment → ortho_tiled.
/// Does not dump trees or walkable.
/// </summary>
internal static class MapDump
{
    private const float DefaultEnrichCell = 0.25f;
    private const float DefaultNamesCell = 2f;
    private const int DefaultOrthoGrid = 8;
    private const int DefaultOrthoTileRes = 1024;

    private static bool _pipelineRunning;

    public static bool IsRunning => _pipelineRunning;

    public static void RunFromConsole(float enrichCell = DefaultEnrichCell, int orthoGrid = DefaultOrthoGrid, int orthoTileRes = DefaultOrthoTileRes)
    {
        if (_pipelineRunning || EnrichmentDump.IsRunning || OrthoDump.IsTiledRunning)
        {
            Msg("dump_map already in progress…");
            return;
        }

        try
        {
            MelonCoroutines.Start(CoPipeline(enrichCell, orthoGrid, orthoTileRes));
        }
        catch (Exception ex)
        {
            MelonLogger.Error($"dump_map failed to start: {ex}");
            try { Il2Cpp.uConsoleLog.Add($"TerrainDumper ERROR: {ex.Message}"); } catch { /* ignore */ }
        }
    }

    private static IEnumerator CoPipeline(float enrichCell, int orthoGrid, int orthoTileRes)
    {
        _pipelineRunning = true;
        DumpBackground.Acquire();
        string sceneName = UnitySceneManager.GetActiveScene().name;
        if (string.IsNullOrWhiteSpace(sceneName))
            sceneName = "UnknownScene";
        string outDir = Path.Combine(MelonEnvironment.ModsDirectory, "TerrainDumper", sceneName);

        Msg($"dump_map start — scene '{sceneName}' names={DefaultNamesCell:G}m enrich={enrichCell:G}m ortho={orthoGrid}x{orthoGrid}@{orthoTileRes}");
        Msg("Reminder: open the charcoal map once in this region before dump_map (fog + alignment).");

        try
        {
            Msg("dump_map [1/4] terrain + fog/alignment…");
            try
            {
                string path = TerrainDump.Run();
                Msg($"dump_map terrain OK → {path}");
            }
            catch (Exception ex)
            {
                MelonLogger.Error($"dump_map terrain failed: {ex}");
                Msg($"dump_map ABORT — terrain failed: {ex.Message}");
                yield break;
            }

            WarnIfAlignmentThin(outDir);
            yield return null;

            Msg($"dump_map [2/4] enrichment names ({DefaultNamesCell:G} m)…");
            try
            {
                EnrichmentDump.RunNamesProbe(DefaultNamesCell);
                Msg("dump_map names OK");
            }
            catch (Exception ex)
            {
                MelonLogger.Error($"dump_map names failed: {ex}");
                Msg($"dump_map ABORT — names failed: {ex.Message}");
                yield break;
            }
            yield return null;

            Msg($"dump_map [3/4] enrichment ({enrichCell:G} m)…");
            EnrichmentDump.Run(enrichCell);
            // Enrichment sets running on Begin; allow a frame if start was deferred
            for (int i = 0; i < 5 && !EnrichmentDump.IsRunning; i++)
                yield return null;

            if (!EnrichmentDump.IsRunning)
            {
                Msg("dump_map ABORT — enrichment did not start (see log).");
                yield break;
            }

            while (EnrichmentDump.IsRunning)
                yield return null;

            Msg("dump_map enrichment OK");
            yield return null;

            Msg($"dump_map [4/4] ortho_tiled {orthoGrid}×{orthoGrid} @ {orthoTileRes}…");
            // OrthoDump clears weather + pins noon before capture.
            OrthoDump.RunTiledFromConsole(orthoGrid, orthoTileRes);

            for (int i = 0; i < 60 && !OrthoDump.IsTiledRunning; i++)
                yield return null;

            if (!OrthoDump.IsTiledRunning)
            {
                Msg("dump_map ABORT — ortho_tiled did not start (see log).");
                yield break;
            }

            while (OrthoDump.IsTiledRunning)
                yield return null;

            Msg($"dump_map COMPLETE → {outDir}");
            Msg("Offline: python tools/make_map_bg.py <that folder> --out out/maps/map_bg_<Scene>_new.png");
        }
        finally
        {
            _pipelineRunning = false;
            DumpBackground.Release();
        }
    }

    private static void WarnIfAlignmentThin(string outDir)
    {
        try
        {
            string fogPath = Path.Combine(outDir, "fog_of_war.json");
            string alignPath = Path.Combine(outDir, "alignment_samples.json");
            if (!File.Exists(fogPath) || !File.Exists(alignPath))
            {
                Msg("WARNING: missing fog_of_war.json or alignment_samples.json — open charcoal map, then re-run dump_map.");
                return;
            }

            string fogText = File.ReadAllText(fogPath);
            if (fogText.Contains("\"fogOfWarFound\": false", StringComparison.OrdinalIgnoreCase) ||
                fogText.Contains("\"panelMapFound\": false", StringComparison.OrdinalIgnoreCase))
            {
                Msg("WARNING: FogOfWar/Panel_Map not fully populated — open charcoal map once, then re-run dump_map.");
            }
        }
        catch (Exception ex)
        {
            MelonLogger.Warning($"dump_map alignment check skipped: {ex.Message}");
        }
    }

    private static void Msg(string text)
    {
        MelonLogger.Msg(text);
        try { Il2Cpp.uConsoleLog.Add($"TerrainDumper: {text}"); } catch { /* ignore */ }
    }
}
