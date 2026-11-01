using System.Reflection;
using System.Runtime.CompilerServices;
using Il2Cpp;
using MelonLoader;
using MelonLoader.Utils;
using UnityEngine;

namespace TerrainDumper;

public class Implementation : MelonMod
{
    public const string ModVersion = "0.9.47";

    private static bool _commandsRegistered;

    internal static bool EnrichmentTicksEnabled;

    public override void OnInitializeMelon()
    {
        MelonLogger.Msg($"Version {ModVersion} loaded (heavy types deferred).");
        MelonLogger.Msg($"Output root: {Path.Combine(MelonEnvironment.ModsDirectory, "TerrainDumper")}");
        MelonLogger.Msg("Console only (no dump hotkeys): dump_map = terrain→names→enrich→ortho");
    }

    public override void OnUpdate()
    {
        TryRegisterCommands();

        if (EnrichmentBatchDump.IsBatchRunning)
            EnrichmentBatchDump.Tick();

        if (EnrichmentTicksEnabled)
            EnrichmentDump.Tick();

        if (Input.GetKeyDown(KeyCode.F1) && Input.GetKey(KeyCode.LeftShift))
        {
            MelonLogger.Msg("Shift+F1 → force_visible toggle");
            ForceVisible.ToggleFromConsole();
        }
    }

    internal static void TryRegisterCommands()
    {
        if (_commandsRegistered)
            return;

        if (uConsole.m_Instance == null)
            return;

        try
        {
            uConsole.RegisterCommand("dump_terrain", new Action(OnDumpTerrain));
            uConsole.RegisterCommand("dump_trees", new Action(OnDumpTrees));
            uConsole.RegisterCommand("dump_enrichment", new Action(OnDumpEnrichment));
            uConsole.RegisterCommand("dump_enrichment_probe", new Action(OnDumpEnrichmentProbe));
            uConsole.RegisterCommand("dump_enrichment_leak", new Action(OnDumpEnrichmentLeak));
            uConsole.RegisterCommand("dump_enrichment_names", new Action(OnDumpEnrichmentNames));
            uConsole.RegisterCommand("dump_enrichment_all", new Action(EnrichmentBatchDump.RunAllFromConsole));
            uConsole.RegisterCommand("dump_enrichment_all_mods", new Action(EnrichmentBatchDump.RunAllModsFromConsole));
            uConsole.RegisterCommand("dump_enrichment_all_abort", new Action(EnrichmentBatchDump.AbortAllFromConsole));
            uConsole.RegisterCommand("dump_walkable", new Action(OnDumpWalkable));
            uConsole.RegisterCommand("dump_portals", new Action(PortalDump.RunFromConsole));
            uConsole.RegisterCommand("dump_pois", new Action(PoisDump.RunFromConsole));
            uConsole.RegisterCommand("dump_fallen_logs", new Action(FallenLogsDump.RunFromConsole));
            uConsole.RegisterCommand("dump_climbing", new Action(ClimbingDump.RunFromConsole));
            uConsole.RegisterCommand("dump_climbing_all", new Action(ClimbingDump.RunAllFromConsole));
            uConsole.RegisterCommand("dump_climbing_all_mods", new Action(ClimbingDump.RunAllModsFromConsole));
            uConsole.RegisterCommand("dump_climbing_all_abort", new Action(ClimbingDump.AbortAllFromConsole));
            uConsole.RegisterCommand("dump_map", new Action(OnDumpMap));
            uConsole.RegisterCommand("dump_ortho", new Action(OnDumpOrtho));
            uConsole.RegisterCommand("dump_ortho_clean", new Action(OnDumpOrthoClean));
            uConsole.RegisterCommand("dump_ortho_tiled", new Action(OnDumpOrthoTiled));
            uConsole.RegisterCommand("dump_ortho_tiled_land", new Action(OnDumpOrthoTiledLand));
            uConsole.RegisterCommand("force_visible", new Action(OnForceVisible));
            _commandsRegistered = true;
            MelonLogger.Msg("Registered: dump_map, dump_terrain, dump_enrichment[_all/_mods], dump_ortho/tiled/tiled_land, dump_walkable, dump_portals, dump_pois, dump_fallen_logs, dump_climbing[_all/_mods], …");
        }
        catch (Exception ex)
        {
            MelonLogger.Error($"Failed to register console commands: {ex}");
            _commandsRegistered = true;
        }
    }

    private static void OnDumpTerrain() => TerrainDump.RunFromConsole();
    private static void OnDumpTrees() => TreeDump.RunFromConsole();

    private static void OnDumpMap()
    {
        float enrichCell = 0.25f;
        int grid = 8;
        int tileRes = 1024;
        try
        {
            int n = uConsole.GetNumParameters();
            if (n >= 1)
                enrichCell = uConsole.GetFloat();
            if (n >= 2)
                grid = Mathf.RoundToInt(uConsole.GetFloat());
            if (n >= 3)
                tileRes = Mathf.RoundToInt(uConsole.GetFloat());
        }
        catch
        {
            enrichCell = 0.25f;
            grid = 8;
            tileRes = 1024;
        }
        MapDump.RunFromConsole(enrichCell, grid, tileRes);
    }

    private static void OnDumpEnrichment()
    {
        float cell = 0.25f;
        int rowsPerFrame = 128;
        try
        {
            int n = uConsole.GetNumParameters();
            if (n >= 1)
                cell = uConsole.GetFloat();
            if (n >= 2)
                rowsPerFrame = Mathf.RoundToInt(uConsole.GetFloat());
        }
        catch
        {
            cell = 0.25f;
            rowsPerFrame = 128;
        }
        EnrichmentDump.Run(cell, rowsPerFrame);
    }

    private static void OnDumpEnrichmentProbe()
    {
        float cell = 8f;
        try
        {
            if (uConsole.GetNumParameters() >= 1)
                cell = uConsole.GetFloat();
        }
        catch { cell = 8f; }
        EnrichmentDump.RunProbe(cell);
    }

    private static void OnDumpEnrichmentLeak()
    {
        float cell = 8f;
        float minAbove = 5f;
        try
        {
            int n = uConsole.GetNumParameters();
            if (n >= 1)
                cell = uConsole.GetFloat();
            if (n >= 2)
                minAbove = uConsole.GetFloat();
        }
        catch
        {
            cell = 8f;
            minAbove = 5f;
        }
        EnrichmentDump.RunLeakProbe(cell, minAbove);
    }

    private static void OnDumpEnrichmentNames()
    {
        float cell = 2f;
        float minAbove = 0.5f;
        try
        {
            int n = uConsole.GetNumParameters();
            if (n >= 1)
                cell = uConsole.GetFloat();
            if (n >= 2)
                minAbove = uConsole.GetFloat();
        }
        catch
        {
            cell = 2f;
            minAbove = 0.5f;
        }
        EnrichmentDump.RunNamesProbe(cell, minAbove);
    }

    private static void OnDumpWalkable()
    {
        float cell = 4f;
        bool rasterize = true;
        try
        {
            int n = uConsole.GetNumParameters();
            if (n >= 1)
                cell = uConsole.GetFloat();
            if (n >= 2)
                rasterize = Mathf.RoundToInt(uConsole.GetFloat()) != 0;
        }
        catch
        {
            cell = 4f;
            rasterize = true;
        }
        WalkableDump.Run(cell, rasterize);
    }

    private static void OnDumpOrtho() => InvokeOrtho(clean: false);
    private static void OnDumpOrthoClean() => InvokeOrtho(clean: true);
    private static void OnForceVisible() => ForceVisible.ToggleFromConsole();

    private static void OnDumpOrthoTiled()
    {
        int grid = 8;
        int tileRes = 1024;
        try
        {
            int n = uConsole.GetNumParameters();
            if (n >= 1)
                grid = Mathf.RoundToInt(uConsole.GetFloat());
            if (n >= 2)
                tileRes = Mathf.RoundToInt(uConsole.GetFloat());
        }
        catch
        {
            grid = 8;
            tileRes = 1024;
        }
        InvokeOrthoTiled(grid, tileRes);
    }

    private static void OnDumpOrthoTiledLand()
    {
        int grid = 8;
        int tileRes = 1024;
        try
        {
            int n = uConsole.GetNumParameters();
            if (n >= 1)
                grid = Mathf.RoundToInt(uConsole.GetFloat());
            if (n >= 2)
                tileRes = Mathf.RoundToInt(uConsole.GetFloat());
        }
        catch
        {
            grid = 8;
            tileRes = 1024;
        }
        InvokeOrthoTiledLand(grid, tileRes);
    }

    [MethodImpl(MethodImplOptions.NoInlining)]
    private static void InvokeOrtho(bool clean)
    {
        try
        {
            Type.GetType("TerrainDumper.OrthoDump, TerrainDumper", throwOnError: false)
                ?.GetMethod("RunFromConsole", BindingFlags.Public | BindingFlags.Static)
                ?.Invoke(null, new object[] { clean });
        }
        catch (Exception ex)
        {
            MelonLogger.Error($"dump_ortho failed: {ex}");
        }
    }

    [MethodImpl(MethodImplOptions.NoInlining)]
    private static void InvokeOrthoTiled(int grid, int tileRes = 1024)
    {
        try
        {
            Type.GetType("TerrainDumper.OrthoDump, TerrainDumper", throwOnError: false)
                ?.GetMethod("RunTiledFromConsole", BindingFlags.Public | BindingFlags.Static)
                ?.Invoke(null, new object[] { grid, tileRes });
        }
        catch (Exception ex)
        {
            MelonLogger.Error($"dump_ortho_tiled failed: {ex}");
        }
    }

    [MethodImpl(MethodImplOptions.NoInlining)]
    private static void InvokeOrthoTiledLand(int grid, int tileRes = 1024)
    {
        try
        {
            Type.GetType("TerrainDumper.OrthoDump, TerrainDumper", throwOnError: false)
                ?.GetMethod("RunTiledLandFromConsole", BindingFlags.Public | BindingFlags.Static)
                ?.Invoke(null, new object[] { grid, tileRes });
        }
        catch (Exception ex)
        {
            MelonLogger.Error($"dump_ortho_tiled_land failed: {ex}");
        }
    }
}
