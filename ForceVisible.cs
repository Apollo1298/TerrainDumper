using MelonLoader;
using UnityEngine;

namespace TerrainDumper;

/// <summary>
/// Session-wide visibility/LOD force so distance-culled objects stay drawn (Map-Maker-Tools style).
/// Does not hijack cameras. Restore with Off() or reload scene.
/// </summary>
internal static class ForceVisible
{
    private static bool _active;
    private static float _lodBias;
    private static List<TerrainRestore>? _terrains;
    private static List<(LODGroup g, int prev)>? _lods;

    public static bool IsActive => _active;

    public static void On()
    {
        if (_active)
        {
            Msg("ForceVisible already on.");
            return;
        }

        try
        {
            _lodBias = QualitySettings.lodBias;
            QualitySettings.lodBias = Math.Max(_lodBias, 100f);

            _terrains = new List<TerrainRestore>();
            Terrain[] terrains = Terrain.activeTerrains;
            if (terrains != null)
            {
                foreach (Terrain t in terrains)
                {
                    if (t == null || t.terrainData == null)
                        continue;
                    _terrains.Add(new TerrainRestore(t));
                    t.drawTreesAndFoliage = true;
                    t.treeDistance = 50000f;
                    t.detailObjectDistance = 5000f;
                    t.basemapDistance = 50000f;
                    t.heightmapPixelError = 1f;
                    try { t.treeBillboardDistance = 50000f; } catch { /* older Unity */ }
                    try { t.treeCrossFadeLength = 0f; } catch { /* ignore */ }
                    try { t.treeMaximumFullLODCount = 100000; } catch { /* ignore */ }
                }
            }

            _lods = new List<(LODGroup, int)>();
            LODGroup[] groups = UnityEngine.Object.FindObjectsOfType<LODGroup>(true);
            if (groups != null)
            {
                foreach (LODGroup g in groups)
                {
                    if (g == null)
                        continue;
                    // -1 = automatic; we can't read prior forced index reliably — store -1 as restore target.
                    _lods.Add((g, -1));
                    try { g.ForceLOD(0); } catch { /* ignore */ }
                }
            }

            _active = true;
            Msg($"ForceVisible ON — terrains={_terrains?.Count ?? 0}, LODGroups={_lods?.Count ?? 0}, lodBias={QualitySettings.lodBias}. Run dump_ortho / dump_ortho_tiled to test.");
        }
        catch (Exception ex)
        {
            MelonLogger.Error($"ForceVisible on failed: {ex}");
            Off();
        }
    }

    public static void Off()
    {
        if (!_active && _terrains == null && _lods == null)
        {
            Msg("ForceVisible already off.");
            return;
        }

        try
        {
            if (_lods != null)
            {
                foreach (var (g, prev) in _lods)
                {
                    if (g == null)
                        continue;
                    try { g.ForceLOD(prev); } catch { /* ignore */ }
                }
            }

            if (_terrains != null)
            {
                foreach (var r in _terrains)
                    r.Restore();
            }

            QualitySettings.lodBias = _lodBias;
        }
        catch (Exception ex)
        {
            MelonLogger.Error($"ForceVisible off failed: {ex}");
        }
        finally
        {
            _lods = null;
            _terrains = null;
            _active = false;
            Msg("ForceVisible OFF — restored.");
        }
    }

    public static void ToggleFromConsole()
    {
        try
        {
            if (Il2Cpp.uConsole.GetNumParameters() >= 1)
            {
                float f = Il2Cpp.uConsole.GetFloat();
                if (f >= 0.5f) On();
                else Off();
                return;
            }
        }
        catch { /* toggle */ }

        if (_active) Off();
        else On();
    }

    private static void Msg(string text)
    {
        MelonLogger.Msg(text);
        try { Il2Cpp.uConsoleLog.Add("TerrainDumper: " + text); } catch { /* ignore */ }
    }

    private sealed class TerrainRestore
    {
        private readonly Terrain _t;
        private readonly bool _draw;
        private readonly float _treeDist;
        private readonly float _detailDist;
        private readonly float _basemap;
        private readonly float _heightErr;
        private readonly float _billboard;
        private readonly float _crossFade;
        private readonly int _maxFullLod;

        public TerrainRestore(Terrain t)
        {
            _t = t;
            _draw = t.drawTreesAndFoliage;
            _treeDist = t.treeDistance;
            _detailDist = t.detailObjectDistance;
            _basemap = t.basemapDistance;
            _heightErr = t.heightmapPixelError;
            float billboard = 50f, cross = 5f;
            int maxFull = 50;
            try { billboard = t.treeBillboardDistance; } catch { /* ignore */ }
            try { cross = t.treeCrossFadeLength; } catch { /* ignore */ }
            try { maxFull = t.treeMaximumFullLODCount; } catch { /* ignore */ }
            _billboard = billboard;
            _crossFade = cross;
            _maxFullLod = maxFull;
        }

        public void Restore()
        {
            if (_t == null)
                return;
            _t.drawTreesAndFoliage = _draw;
            _t.treeDistance = _treeDist;
            _t.detailObjectDistance = _detailDist;
            _t.basemapDistance = _basemap;
            _t.heightmapPixelError = _heightErr;
            try { _t.treeBillboardDistance = _billboard; } catch { /* ignore */ }
            try { _t.treeCrossFadeLength = _crossFade; } catch { /* ignore */ }
            try { _t.treeMaximumFullLODCount = _maxFullLod; } catch { /* ignore */ }
        }
    }
}
