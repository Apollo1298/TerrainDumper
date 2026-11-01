using System.Globalization;
using Il2Cpp;
using MelonLoader;
using UnityEngine;
using UnitySceneManager = UnityEngine.SceneManagement.SceneManager;

namespace TerrainDumper;

/// <summary>
/// Unattended multi-region enrichment. Driven from MelonMod.OnUpdate (not MelonCoroutines)
/// so scene waits + raycast ticks keep running while the game is unfocused.
/// </summary>
internal static class EnrichmentBatchDump
{
    /// <summary>Vanilla outdoor / transition scenes with masks (includes prison survival zone).</summary>
    private static readonly string[] DefaultBatchScenes =
    {
        "AirfieldRegion",
        "AshCanyonRegion",
        "BlackrockPrisonSurvivalZone",
        "BlackrockRegion",
        "BlackrockTransitionZone",
        "CanneryRegion",
        "CanyonRoadTransitionZone",
        "CoastalRegion",
        "CrashMountainRegion",
        "DamRiverTransitionZoneB",
        "HighwayTransitionZone",
        "HubRegion",
        "LakeRegion",
        "LongRailTransitionZone",
        "MarshRegion",
        "MiningRegion",
        "MountainPassRegion",
        "MountainTownRegion",
        "RavineTransitionZone",
        "RiverValleyRegion",
        "RuralRegion",
        "TracksRegion",
        "WhalingStationRegion",
    };

    /// <summary>Mod outdoor scenes that have masks under masks/Mod*.</summary>
    private static readonly string[] DefaultModBatchScenes =
    {
        "ModForsakenShore",
        "ModMountainPass",
        "ModPrecariousCauseway",
        "ModProponentsReach",
        "ModRockyThoroughfare",
        "ModShatteredMarsh",
    };

    private const float SceneLoadTimeoutSec = 120f;
    private const float SceneSettleSec = 4f;
    private const float TerrainWaitSec = 60f;

    private enum Phase
    {
        Idle,
        EnsureScene,
        WaitLoad,
        Settle,
        WaitTerrain,
        StartDump,
        WaitDump,
    }

    private static bool _batchRunning;
    private static bool _batchAbort;
    private static string _label = "dump_enrichment_all";
    private static List<string>? _scenes;
    private static int _index;
    private static int _ok;
    private static int _fail;
    private static float _cellSize = 0.25f;
    private static int _rowsPerFrame = 128;
    private static Phase _phase = Phase.Idle;
    private static float _phaseStartRealtime;
    private static string _currentScene = "";

    public static bool IsBatchRunning => _batchRunning;

    public static void RunAllFromConsole() => StartBatchFromConsole(DefaultBatchScenes, "dump_enrichment_all");

    public static void RunAllModsFromConsole() => StartBatchFromConsole(DefaultModBatchScenes, "dump_enrichment_all_mods");

    private static void StartBatchFromConsole(string[] defaults, string label)
    {
        if (_batchRunning)
        {
            Msg($"{label} already running — dump_enrichment_all_abort to cancel.");
            return;
        }

        if (EnrichmentDump.IsRunning || MapDump.IsRunning || OrthoDump.IsTiledRunning)
        {
            Msg($"{label} blocked — another dump is already in progress.");
            return;
        }

        if (ClimbingDump.IsBatchRunning)
        {
            Msg($"{label} blocked — dump_climbing_all is running.");
            return;
        }

        float cell = 0.25f;
        int rows = 128;
        var scenes = new List<string>();
        try
        {
            var tokens = new List<string>();
            int n = uConsole.GetNumParameters();
            for (int i = 0; i < n; i++)
            {
                string? s = null;
                try { s = uConsole.GetString(); } catch { /* ignore */ }
                if (!string.IsNullOrWhiteSpace(s))
                    tokens.Add(s.Trim());
            }

            int ti = 0;
            if (ti < tokens.Count
                && float.TryParse(tokens[ti], NumberStyles.Float, CultureInfo.InvariantCulture, out float maybeCell)
                && float.IsFinite(maybeCell)
                && maybeCell >= 0.25f
                && maybeCell <= 50f)
            {
                cell = maybeCell;
                ti++;
                if (ti < tokens.Count
                    && int.TryParse(tokens[ti], NumberStyles.Integer, CultureInfo.InvariantCulture, out int maybeRows)
                    && maybeRows >= 1
                    && maybeRows <= 4096)
                {
                    rows = maybeRows;
                    ti++;
                }
            }

            for (; ti < tokens.Count; ti++)
                scenes.Add(tokens[ti]);

            if (scenes.Count == 0)
                scenes.AddRange(defaults);

            _batchRunning = true;
            _batchAbort = false;
            _label = label;
            _scenes = scenes;
            _index = 0;
            _ok = 0;
            _fail = 0;
            _cellSize = cell;
            _rowsPerFrame = rows;
            _phase = Phase.EnsureScene;
            _phaseStartRealtime = Time.realtimeSinceStartup;
            _currentScene = scenes[0];
            DumpBackground.Acquire();
            Msg(
                $"{label} start — {scenes.Count} scene(s) @ {_cellSize}m rows={_rowsPerFrame}. " +
                "Leave the game running (unfocused OK). dump_enrichment_all_abort to cancel.");
            Msg($"{label} [1/{scenes.Count}] → {_currentScene}");
        }
        catch (Exception ex)
        {
            MelonLogger.Error($"{label} failed to start: {ex}");
            try { uConsoleLog.Add($"TerrainDumper ERROR: {ex.Message}"); } catch { /* ignore */ }
            EndBatch();
        }
    }

    public static void AbortAllFromConsole()
    {
        if (!_batchRunning)
        {
            Msg("dump_enrichment_all is not running.");
            return;
        }

        _batchAbort = true;
        if (EnrichmentDump.IsRunning)
            EnrichmentDump.AbortRunning();
        Msg("dump_enrichment_all abort requested — stopping after current step.");
    }

    /// <summary>Called from MelonMod.OnUpdate while the batch is active.</summary>
    public static void Tick()
    {
        if (!_batchRunning || _scenes == null)
            return;

        DumpBackground.Pulse();

        if (_batchAbort)
        {
            Msg($"{_label} aborted after {_ok} ok / {_fail} fail.");
            EndBatch();
            return;
        }

        try
        {
            switch (_phase)
            {
                case Phase.EnsureScene:
                    TickEnsureScene();
                    break;
                case Phase.WaitLoad:
                    TickWaitLoad();
                    break;
                case Phase.Settle:
                    TickSettle();
                    break;
                case Phase.WaitTerrain:
                    TickWaitTerrain();
                    break;
                case Phase.StartDump:
                    TickStartDump();
                    break;
                case Phase.WaitDump:
                    TickWaitDump();
                    break;
            }
        }
        catch (Exception ex)
        {
            _fail++;
            MelonLogger.Error($"{_label} tick failed in {_currentScene}: {ex}");
            Msg($"{_label} FAIL {_currentScene}: {ex.Message}");
            AdvanceOrFinish();
        }
    }

    private static void TickEnsureScene()
    {
        if (_scenes == null || _index >= _scenes.Count)
        {
            FinishBatch();
            return;
        }

        _currentScene = _scenes[_index];
        if (SceneMatches(_currentScene))
        {
            Msg($"Already in {_currentScene} — settle then dump.");
            Enter(Phase.Settle);
            return;
        }

        if (!TryBeginSceneLoad(_currentScene))
        {
            _fail++;
            Msg($"{_label} SKIP {_currentScene} — could not start load.");
            AdvanceOrFinish();
            return;
        }

        Enter(Phase.WaitLoad);
    }

    private static void TickWaitLoad()
    {
        if (SceneMatches(_currentScene) && !IsSceneLoadBusy())
        {
            Enter(Phase.Settle);
            return;
        }

        if (Time.realtimeSinceStartup - _phaseStartRealtime >= SceneLoadTimeoutSec)
        {
            MelonLogger.Warning(
                $"{_label}: timed out waiting for '{_currentScene}' (active='{CurrentSceneName()}', busy={IsSceneLoadBusy()})");
            _fail++;
            Msg($"{_label} SKIP {_currentScene} — load timed out.");
            AdvanceOrFinish();
        }
    }

    private static void TickSettle()
    {
        if (IsSceneLoadBusy())
        {
            // Hold settle clock until async loads quiet, but cap total settle+busy.
            if (Time.realtimeSinceStartup - _phaseStartRealtime < SceneSettleSec + 30f)
                return;
        }
        else if (Time.realtimeSinceStartup - _phaseStartRealtime < SceneSettleSec)
        {
            return;
        }

        if (!SceneMatches(_currentScene))
        {
            MelonLogger.Warning(
                $"{_label}: scene drifted during settle (want '{_currentScene}', have '{CurrentSceneName()}')");
            _fail++;
            Msg($"{_label} SKIP {_currentScene} — scene drifted.");
            AdvanceOrFinish();
            return;
        }

        Enter(Phase.WaitTerrain);
    }

    private static void TickWaitTerrain()
    {
        if (HasUsableTerrain())
        {
            Enter(Phase.StartDump);
            return;
        }

        if (Time.realtimeSinceStartup - _phaseStartRealtime >= TerrainWaitSec)
        {
            _fail++;
            Msg($"{_label} SKIP {_currentScene} — no active Terrain after settle.");
            AdvanceOrFinish();
        }
    }

    private static void TickStartDump()
    {
        try
        {
            EnrichmentDump.Run(_cellSize, _rowsPerFrame);
        }
        catch (Exception ex)
        {
            _fail++;
            MelonLogger.Error($"{_label} dump failed to start in {_currentScene}: {ex}");
            Msg($"{_label} FAIL {_currentScene}: {ex.Message}");
            AdvanceOrFinish();
            return;
        }

        if (!EnrichmentDump.IsRunning)
        {
            _fail++;
            Msg($"{_label} FAIL {_currentScene} — enrichment did not start.");
            AdvanceOrFinish();
            return;
        }

        Enter(Phase.WaitDump);
    }

    private static void TickWaitDump()
    {
        if (EnrichmentDump.IsRunning)
            return;

        _ok++;
        Msg($"{_label} OK {_currentScene} ({_ok} ok / {_fail} fail)");
        AdvanceOrFinish();
    }

    private static void AdvanceOrFinish()
    {
        if (_scenes == null)
        {
            FinishBatch();
            return;
        }

        _index++;
        if (_index >= _scenes.Count || _batchAbort)
        {
            FinishBatch();
            return;
        }

        Msg($"{_label} [{_index + 1}/{_scenes.Count}] → {_scenes[_index]}");
        Enter(Phase.EnsureScene);
    }

    private static void FinishBatch()
    {
        Msg($"{_label} COMPLETE — ok={_ok}, fail={_fail}, total={_scenes?.Count ?? 0}");
        EndBatch();
    }

    private static void EndBatch()
    {
        if (_batchRunning)
            DumpBackground.Release();
        _batchRunning = false;
        _batchAbort = false;
        _scenes = null;
        _phase = Phase.Idle;
        _currentScene = "";
    }

    private static void Enter(Phase phase)
    {
        _phase = phase;
        _phaseStartRealtime = Time.realtimeSinceStartup;
    }

    private static bool HasUsableTerrain()
    {
        try
        {
            Terrain[]? terrains = Terrain.activeTerrains;
            if (terrains == null || terrains.Length == 0)
                return false;
            foreach (Terrain t in terrains)
            {
                if (t != null && t.terrainData != null)
                    return true;
            }
        }
        catch { /* ignore */ }
        return false;
    }

    private static bool TryBeginSceneLoad(string scene)
    {
        try
        {
            GameManager.LoadSceneWithLoadingScreen(scene);
            DumpBackground.Pulse();
            Msg($"LoadSceneWithLoadingScreen('{scene}')");
            return true;
        }
        catch (Exception ex)
        {
            MelonLogger.Warning($"LoadSceneWithLoadingScreen failed ({ex.Message}); trying console scene…");
        }

        try
        {
            uConsole.RunCommand("scene " + scene);
            DumpBackground.Pulse();
            Msg($"uConsole scene {scene}");
            return true;
        }
        catch (Exception ex)
        {
            MelonLogger.Error($"{_label}: could not start load for '{scene}': {ex}");
            return false;
        }
    }

    private static bool SceneMatches(string want)
    {
        try
        {
            if (GameManager.IsActiveScene(want))
                return true;
        }
        catch { /* ignore */ }

        try
        {
            if (GameManager.CompareSceneNames(CurrentSceneName(), want))
                return true;
        }
        catch { /* ignore */ }

        return string.Equals(CurrentSceneName(), want, StringComparison.OrdinalIgnoreCase);
    }

    private static string CurrentSceneName()
    {
        try
        {
            string? active = GameManager.m_ActiveScene;
            if (!string.IsNullOrWhiteSpace(active))
                return active;
        }
        catch { /* ignore */ }

        try
        {
            return UnitySceneManager.GetActiveScene().name ?? "";
        }
        catch
        {
            return "";
        }
    }

    private static bool IsSceneLoadBusy()
    {
        try
        {
            var dict = GameManager.m_AsyncSceneLoadsInProgress;
            if (dict != null && dict.Count > 0)
                return true;
        }
        catch { /* ignore */ }

        try
        {
            Panel_Loading? panel = UnityEngine.Object.FindObjectOfType<Panel_Loading>();
            if (panel != null)
            {
                try
                {
                    if (panel.IsLoading())
                        return true;
                }
                catch { /* ignore */ }

                try
                {
                    if (panel.HasPendingSceneLoads)
                        return true;
                }
                catch { /* ignore */ }
            }
        }
        catch { /* ignore */ }

        return false;
    }

    private static void Msg(string text)
    {
        MelonLogger.Msg(text);
        try { uConsoleLog.Add("TerrainDumper: " + text); } catch { /* ignore */ }
    }
}
