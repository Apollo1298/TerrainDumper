using System.Globalization;
using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;
using Il2CppInterop.Runtime;
using Il2CppInterop.Runtime.InteropTypes.Arrays;
using MelonLoader;
using MelonLoader.Utils;
using UnityEngine;
using UnitySceneManager = UnityEngine.SceneManagement.SceneManager;

namespace TerrainDumper;

/// <summary>
/// Raycast height grid over active terrain bounds (collider enrichment for map art).
/// Runs across frames so the game stays responsive.
/// Skips known invisible barrier layers / COLLISION GROUP ancestors.
/// Labels each accepted hit with a small class id for offline map overlay decisions.
/// </summary>
internal static class EnrichmentDump
{
    private const float DefaultCellSize = 0.25f;
    private const float MinCellSize = 0.25f;
    private const float MaxCellSize = 50f;
    /// <summary>
    /// Rows of the Z-grid processed per Unity frame. AFK dumps can push this high;
    /// too high freezes a single frame for many seconds (looks hung). 128 ≈ 40× fewer
    /// yields than the old value of 3, same ray count / resolution.
    /// </summary>
    private const int DefaultRowsPerFrame = 128;
    private const int MinRowsPerFrame = 1;
    private const int MaxRowsPerFrame = 4096;
    private const int HitBufferSize = 32;
    private const float DefaultProbeCellSize = 8f;
    private const float DefaultNamesProbeCellSize = 2f;
    private const float DefaultLeakMinAboveMeters = 5f;

    /// <summary>Per-cell class ids written to enrichment_class.raw (must match ClassLabelNames).</summary>
    internal enum HitClass : byte
    {
        None = 0,
        Terrain = 1,
        Rock = 2,
        /// <summary>Bridges, docks, buildings, log piles — thin rim in map build.</summary>
        Structure = 3,
        IceBackdrop = 4,
        Ignore = 5,
    }

    private static readonly string[] ClassLabelNames =
    {
        "none",
        "terrain",
        "rock",
        "structure",
        "ice_backdrop",
        "ignore",
    };

    private static bool _running;
    private static string _outDir = "";
    private static string _sceneName = "";
    private static float _cellSize = DefaultCellSize;
    private static int _rowsPerFrame = DefaultRowsPerFrame;
    private static float _originX;
    private static float _originZ;
    private static float _minY;
    private static float _maxY;
    private static int _width;
    private static int _height;
    private static int _row;
    private static float[]? _heights; // row-major, NaN = no hit
    private static byte[]? _classes; // row-major HitClass ids
    private static int _hits;
    private static int _rays;
    private static int _skippedInvisible;
    private static float _rayStartY;
    private static float _rayDist;
    private static Il2CppStructArray<RaycastHit>? _hitBuf;
    /// <summary>Collider.GetInstanceID() → is invisible wall. Cleared each dump/probe.</summary>
    private static Dictionary<int, bool>? _wallCache;
    /// <summary>Collider.GetInstanceID() → HitClass. Cleared each dump/probe.</summary>
    private static Dictionary<int, byte>? _classCache;
    private static int _layerCharControllerOnly = -2; // -2 = unresolved, -1 = missing
    private static int _layerNoCollidePlayer = -2;
    private static int _layerParticleKiller = -2;
    private static int _layerPlayer = -2;
    private static int _layerTriggerReverb = -2;
    private static int _layerTriggerIgnoreRaycast = -2;
    /// <summary>DefaultRaycastLayers minus known non-ground lids (e.g. TriggerIgnoreRaycast wind boxes).</summary>
    private static int _raycastMask = Physics.DefaultRaycastLayers;

    private const string SkipRuleDescription =
        "CharacterControllerCollideOnly / NoCollidePlayer / ParticleKiller / Player / TriggerReverb / " +
        "TriggerIgnoreRaycast layers; " +
        "or ancestor named COLLISION GROUP / InvivibleCollision / InvisibleCollision / " +
        "Prologue Collision / Collsion / WindSpeedTriggers";

    private const string ClassRuleDescription =
        "TerrainCollider→terrain; man-made name/path (Bridge/Hangar/Cabin/Dam/Quonset/…/STR_/BLD_)→structure; " +
        "Rock/Boulder/Cliff→rock; Ice/Water/Pond/Creek/Shelf/Backdrop→ice_backdrop; " +
        "unmatched Box/Capsule/Sphere→ignore; else→terrain";

    public static bool IsRunning => _running;

    /// <summary>
    /// Start enrichment. Call from console handler (after parsing args) or F10 with default cell.
    /// Do not call uConsole.GetFloat here — hotkey path must not touch console args.
    /// </summary>
    public static void Run(float cellSize = DefaultCellSize, int rowsPerFrame = DefaultRowsPerFrame)
    {
        if (_running)
        {
            Msg("Enrichment dump already in progress…");
            return;
        }

        try
        {
            if (!float.IsFinite(cellSize) || cellSize < MinCellSize || cellSize > MaxCellSize)
            {
                Msg($"dump_enrichment: cell size must be {MinCellSize}..{MaxCellSize} m (got {cellSize}). Usage: dump_enrichment [cellMeters] [rowsPerFrame]");
                return;
            }
            if (rowsPerFrame < MinRowsPerFrame || rowsPerFrame > MaxRowsPerFrame)
            {
                Msg($"dump_enrichment: rowsPerFrame must be {MinRowsPerFrame}..{MaxRowsPerFrame} (got {rowsPerFrame}).");
                return;
            }

            Begin(cellSize, rowsPerFrame);
        }
        catch (Exception ex)
        {
            MelonLogger.Error($"dump_enrichment failed to start: {ex}");
            try { Il2Cpp.uConsoleLog.Add($"TerrainDumper ERROR: {ex.Message}"); } catch { /* ignore */ }
        }
    }

    /// <summary>
    /// Coarse grid: log unique invisible-wall-like colliders that sit above an accepted ground hit.
    /// Writes enrichment_wall_probe.json; does not overwrite enrichment heights.
    /// </summary>
    public static void RunProbe(float cellSize = DefaultProbeCellSize)
    {
        if (_running)
        {
            Msg("Enrichment dump already in progress — probe aborted.");
            return;
        }

        try
        {
            if (!float.IsFinite(cellSize) || cellSize < MinCellSize || cellSize > MaxCellSize)
            {
                Msg($"dump_enrichment_probe: cell size must be {MinCellSize}..{MaxCellSize} m (got {cellSize}).");
                return;
            }

            ComputeTerrainBounds(out float minX, out float maxX, out float minZ, out float maxZ, out float minY, out float maxY);

            string sceneName = UnitySceneManager.GetActiveScene().name;
            if (string.IsNullOrWhiteSpace(sceneName))
                sceneName = "UnknownScene";
            string outDir = Path.Combine(MelonEnvironment.ModsDirectory, "TerrainDumper", sceneName);
            Directory.CreateDirectory(outDir);

            float rayStartY = maxY + 250f;
            float rayDist = (maxY - minY) + 500f;
            int width = Math.Max(2, Mathf.FloorToInt((maxX - minX) / cellSize) + 1);
            int height = Math.Max(2, Mathf.FloorToInt((maxZ - minZ) / cellSize) + 1);

        EnsureHitBuffer();
        ClearWallCache();
        ResolveBarrierLayers();
        var unique = new Dictionary<string, ProbeEntry>(StringComparer.Ordinal);
            int rays = 0, wallRays = 0;

            Msg($"Enrichment wall probe: {width}x{height} @ {cellSize}m…");

            for (int iz = 0; iz < height; iz++)
            {
                for (int ix = 0; ix < width; ix++)
                {
                    float x = minX + ix * cellSize;
                    float z = minZ + iz * cellSize;
                    var origin = new Vector3(x, rayStartY, z);
                    rays++;
                    int n = Physics.RaycastNonAlloc(
                        origin, Vector3.down, _hitBuf, rayDist,
                        _raycastMask, QueryTriggerInteraction.Ignore);
                    if (n <= 0)
                        continue;

                    bool sawWall = false;
                    int lim = Math.Min(n, HitBufferSize);
                    for (int i = 0; i < lim; i++)
                    {
                        RaycastHit hit = _hitBuf![i];
                        Collider? col = hit.collider;
                        if (col == null || !IsInvisibleWallColliderCached(col))
                            continue;

                        sawWall = true;
                        string key = ProbeKey(col);
                        if (!unique.TryGetValue(key, out ProbeEntry? e))
                        {
                            e = new ProbeEntry
                            {
                                Key = key,
                                Name = col.gameObject != null ? col.gameObject.name : "(null)",
                                Path = GameObjectPath(col.gameObject),
                                ColliderType = ColliderTypeName(col),
                                Layer = col.gameObject != null ? col.gameObject.layer : -1,
                                LayerName = col.gameObject != null
                                    ? LayerMask.LayerToName(col.gameObject.layer)
                                    : "",
                                Count = 0,
                            };
                            unique[key] = e;
                        }
                        e.Count++;
                        e.ExampleY = hit.point.y;
                        e.ExampleX = hit.point.x;
                        e.ExampleZ = hit.point.z;
                    }

                    if (sawWall)
                        wallRays++;
                }
            }

            var list = unique.Values.OrderByDescending(e => e.Count).ToList();
            var payload = new
            {
                sceneName,
                dumpedAtUtc = DateTime.UtcNow.ToString("o", CultureInfo.InvariantCulture),
                modVersion = Implementation.ModVersion,
                cellSize,
                width,
                height,
                rays,
                wallRays,
                uniqueColliders = list.Count,
                skipRule = SkipRuleDescription,
                entries = list.Take(200).Select(e => new
                {
                    e.Name,
                    e.Path,
                    e.ColliderType,
                    e.Layer,
                    e.LayerName,
                    e.Count,
                    example = new { x = e.ExampleX, y = e.ExampleY, z = e.ExampleZ },
                }),
            };

            string path = Path.Combine(outDir, "enrichment_wall_probe.json");
            File.WriteAllText(
                path,
                JsonSerializer.Serialize(payload, JsonOpts),
                new UTF8Encoding(encoderShouldEmitUTF8Identifier: false));

            Msg($"Wall probe done: wallRays={wallRays}/{rays}, unique={list.Count} → {path}");
            int show = Math.Min(15, list.Count);
            for (int i = 0; i < show; i++)
            {
                ProbeEntry e = list[i];
                MelonLogger.Msg(
                    $"  [{e.Count}] {e.ColliderType} layer={e.LayerName}({e.Layer}) {e.Path}");
            }
        }
        catch (Exception ex)
        {
            MelonLogger.Error($"dump_enrichment_probe failed: {ex}");
            try { Il2Cpp.uConsoleLog.Add($"TerrainDumper ERROR: {ex.Message}"); } catch { /* ignore */ }
        }
    }

    /// <summary>
    /// Full-region coarse grid: find Box/Capsule/Sphere hits that enrichment would ACCEPT
    /// (not matching the invisible-wall skip rule) but sit well above terrain/ground below.
    /// Those are the square plateaus leaking into enrichment_heights.raw.
    /// Writes enrichment_leak_probe.json; does not overwrite enrichment heights.
    /// </summary>
    public static void RunLeakProbe(
        float cellSize = DefaultProbeCellSize,
        float minAboveMeters = DefaultLeakMinAboveMeters)
    {
        if (_running)
        {
            Msg("Enrichment dump already in progress — leak probe aborted.");
            return;
        }

        try
        {
            if (!float.IsFinite(cellSize) || cellSize < MinCellSize || cellSize > MaxCellSize)
            {
                Msg($"dump_enrichment_leak: cell size must be {MinCellSize}..{MaxCellSize} m (got {cellSize}).");
                return;
            }
            if (!float.IsFinite(minAboveMeters) || minAboveMeters < 0.5f || minAboveMeters > 200f)
            {
                Msg($"dump_enrichment_leak: minAbove must be 0.5..200 m (got {minAboveMeters}).");
                return;
            }

            ComputeTerrainBounds(out float minX, out float maxX, out float minZ, out float maxZ, out float minY, out float maxY);

            string sceneName = UnitySceneManager.GetActiveScene().name;
            if (string.IsNullOrWhiteSpace(sceneName))
                sceneName = "UnknownScene";
            string outDir = Path.Combine(MelonEnvironment.ModsDirectory, "TerrainDumper", sceneName);
            Directory.CreateDirectory(outDir);

            float rayStartY = maxY + 250f;
            float rayDist = (maxY - minY) + 500f;
            int width = Math.Max(2, Mathf.FloorToInt((maxX - minX) / cellSize) + 1);
            int height = Math.Max(2, Mathf.FloorToInt((maxZ - minZ) / cellSize) + 1);

            EnsureHitBuffer();
            ClearWallCache();
            ResolveBarrierLayers();
            var unique = new Dictionary<string, LeakProbeEntry>(StringComparer.Ordinal);
            int rays = 0, leakRays = 0;
            var sorted = new List<(float dist, RaycastHit hit)>(HitBufferSize);

            Msg(
                $"Enrichment leak probe: {width}x{height} @ {cellSize}m, " +
                $"minAbove={minAboveMeters:F1}m (accepted primitive ≫ ground)…");

            for (int iz = 0; iz < height; iz++)
            {
                for (int ix = 0; ix < width; ix++)
                {
                    float x = minX + ix * cellSize;
                    float z = minZ + iz * cellSize;
                    var origin = new Vector3(x, rayStartY, z);
                    rays++;
                    int n = Physics.RaycastNonAlloc(
                        origin, Vector3.down, _hitBuf, rayDist,
                        _raycastMask, QueryTriggerInteraction.Ignore);
                    if (n <= 0)
                        continue;

                    sorted.Clear();
                    int lim = Math.Min(n, HitBufferSize);
                    for (int i = 0; i < lim; i++)
                    {
                        RaycastHit hit = _hitBuf![i];
                        if (hit.collider == null)
                            continue;
                        sorted.Add((hit.distance, hit));
                    }
                    if (sorted.Count == 0)
                        continue;
                    sorted.Sort((a, b) => a.dist.CompareTo(b.dist));

                    // Same accept rule as enrichment: first non-wall hit wins.
                    Collider? acceptedCol = null;
                    RaycastHit acceptedHit = default;
                    int acceptedIdx = -1;
                    for (int i = 0; i < sorted.Count; i++)
                    {
                        Collider? col = sorted[i].hit.collider;
                        if (col == null)
                            continue;
                        if (col.TryCast<TerrainCollider>() != null)
                        {
                            acceptedCol = col;
                            acceptedHit = sorted[i].hit;
                            acceptedIdx = i;
                            break;
                        }
                        if (IsInvisibleWallColliderCached(col))
                            continue;
                        acceptedCol = col;
                        acceptedHit = sorted[i].hit;
                        acceptedIdx = i;
                        break;
                    }

                    if (acceptedCol == null || acceptedIdx < 0)
                        continue;
                    // Terrain-first cells are fine; only primitive proxies make square plateaus.
                    if (!IsPrimitiveProxyCollider(acceptedCol))
                        continue;

                    // Ground = nearest TerrainCollider below the accepted hit (else next non-wall).
                    float groundY = float.NaN;
                    for (int i = acceptedIdx + 1; i < sorted.Count; i++)
                    {
                        Collider? col = sorted[i].hit.collider;
                        if (col == null)
                            continue;
                        if (col.TryCast<TerrainCollider>() != null)
                        {
                            groundY = sorted[i].hit.point.y;
                            break;
                        }
                    }
                    if (!float.IsFinite(groundY))
                    {
                        for (int i = acceptedIdx + 1; i < sorted.Count; i++)
                        {
                            Collider? col = sorted[i].hit.collider;
                            if (col == null || IsInvisibleWallColliderCached(col))
                                continue;
                            groundY = sorted[i].hit.point.y;
                            break;
                        }
                    }
                    if (!float.IsFinite(groundY))
                        continue;

                    float above = acceptedHit.point.y - groundY;
                    if (above < minAboveMeters)
                        continue;

                    leakRays++;
                    string key = ProbeKey(acceptedCol);
                    if (!unique.TryGetValue(key, out LeakProbeEntry? e))
                    {
                        GetColliderApproxSize(acceptedCol, out float sx, out float sy, out float sz);
                        e = new LeakProbeEntry
                        {
                            Key = key,
                            Name = acceptedCol.gameObject != null ? acceptedCol.gameObject.name : "(null)",
                            Path = GameObjectPath(acceptedCol.gameObject),
                            ColliderType = ColliderTypeName(acceptedCol),
                            Layer = acceptedCol.gameObject != null ? acceptedCol.gameObject.layer : -1,
                            LayerName = acceptedCol.gameObject != null
                                ? LayerMask.LayerToName(acceptedCol.gameObject.layer)
                                : "",
                            HasRenderer = HasVisualRenderer(acceptedCol.gameObject),
                            ProposedClass = ClassLabelNames[(int)ClassifyHit(acceptedCol)],
                            SizeX = sx,
                            SizeY = sy,
                            SizeZ = sz,
                            Count = 0,
                            MaxAboveMeters = 0f,
                        };
                        unique[key] = e;
                    }
                    e.Count++;
                    if (above > e.MaxAboveMeters)
                    {
                        e.MaxAboveMeters = above;
                        e.ExampleX = acceptedHit.point.x;
                        e.ExampleY = acceptedHit.point.y;
                        e.ExampleZ = acceptedHit.point.z;
                        e.ExampleGroundY = groundY;
                    }
                }
            }

            var list = unique.Values.OrderByDescending(e => e.Count).ToList();
            var payload = new
            {
                sceneName,
                dumpedAtUtc = DateTime.UtcNow.ToString("o", CultureInfo.InvariantCulture),
                modVersion = Implementation.ModVersion,
                cellSize,
                minAboveMeters,
                width,
                height,
                rays,
                leakRays,
                uniqueColliders = list.Count,
                rule =
                    "Accepted (not skipped by invisible-wall rule) Box/Capsule/Sphere whose hit is " +
                    $">={minAboveMeters:F1}m above terrain/ground below — candidates for square enrichment plateaus.",
                skipRule = SkipRuleDescription,
                classRule = ClassRuleDescription,
                entries = list.Take(200).Select(e => new
                {
                    e.Name,
                    e.Path,
                    e.ColliderType,
                    e.Layer,
                    e.LayerName,
                    e.HasRenderer,
                    e.ProposedClass,
                    size = new { x = e.SizeX, y = e.SizeY, z = e.SizeZ },
                    e.Count,
                    maxAboveMeters = e.MaxAboveMeters,
                    example = new
                    {
                        x = e.ExampleX,
                        y = e.ExampleY,
                        z = e.ExampleZ,
                        groundY = e.ExampleGroundY,
                    },
                }),
            };

            string path = Path.Combine(outDir, "enrichment_leak_probe.json");
            File.WriteAllText(
                path,
                JsonSerializer.Serialize(payload, JsonOpts),
                new UTF8Encoding(encoderShouldEmitUTF8Identifier: false));

            Msg($"Leak probe done: leakRays={leakRays}/{rays}, unique={list.Count} → {path}");
            int show = Math.Min(20, list.Count);
            for (int i = 0; i < show; i++)
            {
                LeakProbeEntry e = list[i];
                MelonLogger.Msg(
                    $"  [{e.Count}] +{e.MaxAboveMeters:F1}m {e.ColliderType} " +
                    $"class={e.ProposedClass} rend={e.HasRenderer} layer={e.LayerName}({e.Layer}) " +
                    $"size=({e.SizeX:F1},{e.SizeY:F1},{e.SizeZ:F1}) {e.Path}");
            }
        }
        catch (Exception ex)
        {
            MelonLogger.Error($"dump_enrichment_leak failed: {ex}");
            try { Il2Cpp.uConsoleLog.Add($"TerrainDumper ERROR: {ex.Message}"); } catch { /* ignore */ }
        }
    }

    /// <summary>
    /// Coarse grid: log unique non-terrain colliders that enrichment would ACCEPT
    /// (MeshCollider included). Use to verify structure name tokens (cars, hangars, etc.).
    /// Optional minAbove filters to hits that sit above ground (default 0.5 m).
    /// Writes enrichment_names.json; does not overwrite enrichment heights.
    /// </summary>
    public static void RunNamesProbe(
        float cellSize = DefaultNamesProbeCellSize,
        float minAboveMeters = 0.5f)
    {
        if (_running)
        {
            Msg("Enrichment dump already in progress — names probe aborted.");
            return;
        }

        try
        {
            if (!float.IsFinite(cellSize) || cellSize < MinCellSize || cellSize > MaxCellSize)
            {
                Msg($"dump_enrichment_names: cell size must be {MinCellSize}..{MaxCellSize} m (got {cellSize}).");
                return;
            }
            if (!float.IsFinite(minAboveMeters) || minAboveMeters < 0f || minAboveMeters > 200f)
            {
                Msg($"dump_enrichment_names: minAbove must be 0..200 m (got {minAboveMeters}).");
                return;
            }

            ComputeTerrainBounds(out float minX, out float maxX, out float minZ, out float maxZ, out float minY, out float maxY);

            string sceneName = UnitySceneManager.GetActiveScene().name;
            if (string.IsNullOrWhiteSpace(sceneName))
                sceneName = "UnknownScene";
            string outDir = Path.Combine(MelonEnvironment.ModsDirectory, "TerrainDumper", sceneName);
            Directory.CreateDirectory(outDir);

            float rayStartY = maxY + 250f;
            float rayDist = (maxY - minY) + 500f;
            int width = Math.Max(2, Mathf.FloorToInt((maxX - minX) / cellSize) + 1);
            int height = Math.Max(2, Mathf.FloorToInt((maxZ - minZ) / cellSize) + 1);

            EnsureHitBuffer();
            ClearWallCache();
            ResolveBarrierLayers();
            var unique = new Dictionary<string, LeakProbeEntry>(StringComparer.Ordinal);
            int rays = 0, namedRays = 0;
            var sorted = new List<(float dist, RaycastHit hit)>(HitBufferSize);

            Msg(
                $"Enrichment names probe: {width}x{height} @ {cellSize}m, " +
                $"minAbove={minAboveMeters:F1}m (accepted non-terrain hits)…");

            for (int iz = 0; iz < height; iz++)
            {
                for (int ix = 0; ix < width; ix++)
                {
                    float x = minX + ix * cellSize;
                    float z = minZ + iz * cellSize;
                    var origin = new Vector3(x, rayStartY, z);
                    rays++;
                    int n = Physics.RaycastNonAlloc(
                        origin, Vector3.down, _hitBuf, rayDist,
                        _raycastMask, QueryTriggerInteraction.Ignore);
                    if (n <= 0)
                        continue;

                    sorted.Clear();
                    int lim = Math.Min(n, HitBufferSize);
                    for (int i = 0; i < lim; i++)
                    {
                        RaycastHit hit = _hitBuf![i];
                        if (hit.collider == null)
                            continue;
                        sorted.Add((hit.distance, hit));
                    }
                    if (sorted.Count == 0)
                        continue;
                    sorted.Sort((a, b) => a.dist.CompareTo(b.dist));

                    Collider? acceptedCol = null;
                    RaycastHit acceptedHit = default;
                    int acceptedIdx = -1;
                    for (int i = 0; i < sorted.Count; i++)
                    {
                        Collider? col = sorted[i].hit.collider;
                        if (col == null)
                            continue;
                        if (col.TryCast<TerrainCollider>() != null)
                        {
                            acceptedCol = col;
                            acceptedHit = sorted[i].hit;
                            acceptedIdx = i;
                            break;
                        }
                        if (IsInvisibleWallColliderCached(col))
                            continue;
                        acceptedCol = col;
                        acceptedHit = sorted[i].hit;
                        acceptedIdx = i;
                        break;
                    }

                    if (acceptedCol == null || acceptedIdx < 0)
                        continue;
                    // Terrain is the common case — skip; we want named props/meshes.
                    if (acceptedCol.TryCast<TerrainCollider>() != null)
                        continue;

                    float groundY = float.NaN;
                    for (int i = acceptedIdx + 1; i < sorted.Count; i++)
                    {
                        Collider? col = sorted[i].hit.collider;
                        if (col == null)
                            continue;
                        if (col.TryCast<TerrainCollider>() != null)
                        {
                            groundY = sorted[i].hit.point.y;
                            break;
                        }
                    }
                    if (!float.IsFinite(groundY))
                    {
                        for (int i = acceptedIdx + 1; i < sorted.Count; i++)
                        {
                            Collider? col = sorted[i].hit.collider;
                            if (col == null || IsInvisibleWallColliderCached(col))
                                continue;
                            groundY = sorted[i].hit.point.y;
                            break;
                        }
                    }
                    if (!float.IsFinite(groundY))
                        continue;

                    float above = acceptedHit.point.y - groundY;
                    if (above < minAboveMeters)
                        continue;

                    namedRays++;
                    string key = ProbeKey(acceptedCol);
                    if (!unique.TryGetValue(key, out LeakProbeEntry? e))
                    {
                        GetColliderApproxSize(acceptedCol, out float sx, out float sy, out float sz);
                        e = new LeakProbeEntry
                        {
                            Key = key,
                            Name = acceptedCol.gameObject != null ? acceptedCol.gameObject.name : "(null)",
                            Path = GameObjectPath(acceptedCol.gameObject),
                            ColliderType = ColliderTypeName(acceptedCol),
                            Layer = acceptedCol.gameObject != null ? acceptedCol.gameObject.layer : -1,
                            LayerName = acceptedCol.gameObject != null
                                ? LayerMask.LayerToName(acceptedCol.gameObject.layer)
                                : "",
                            HasRenderer = HasVisualRenderer(acceptedCol.gameObject),
                            ProposedClass = ClassLabelNames[(int)ClassifyHit(acceptedCol)],
                            SizeX = sx,
                            SizeY = sy,
                            SizeZ = sz,
                            Count = 0,
                            MaxAboveMeters = 0f,
                        };
                        unique[key] = e;
                    }
                    e.Count++;
                    if (above > e.MaxAboveMeters)
                    {
                        e.MaxAboveMeters = above;
                        e.ExampleX = acceptedHit.point.x;
                        e.ExampleY = acceptedHit.point.y;
                        e.ExampleZ = acceptedHit.point.z;
                        e.ExampleGroundY = groundY;
                    }
                }
            }

            var list = unique.Values.OrderByDescending(e => e.Count).ToList();
            WriteNamesJson(
                outDir,
                sceneName,
                "enrichment_names.json",
                cellSize,
                minAboveMeters,
                width,
                height,
                rays,
                namedRays,
                list,
                "Accepted enrichment hits that are not TerrainCollider, optionally " +
                $">={minAboveMeters:F1}m above ground below — MeshCollider included. " +
                "Use to verify structure name tokens.");

            string path = Path.Combine(outDir, "enrichment_names.json");
            Msg(
                $"Names probe done: namedRays={namedRays}/{rays}, unique={list.Count} → {path}");
            int show = Math.Min(15, list.Count);
            for (int i = 0; i < show; i++)
            {
                LeakProbeEntry e = list[i];
                MelonLogger.Msg(
                    $"  [{e.Count}] +{e.MaxAboveMeters:F1}m {e.ColliderType} " +
                    $"class={e.ProposedClass} {e.Path}");
            }
            var vehicleSample = list
                .Where(e =>
                {
                    string path = e.Path;
                    if (path.IndexOf("Carcass", StringComparison.OrdinalIgnoreCase) >= 0)
                        return false;
                    return path.IndexOf("Car", StringComparison.OrdinalIgnoreCase) >= 0
                        || path.IndexOf("Vehicle", StringComparison.OrdinalIgnoreCase) >= 0
                        || path.IndexOf("Truck", StringComparison.OrdinalIgnoreCase) >= 0
                        || path.IndexOf("Bus", StringComparison.OrdinalIgnoreCase) >= 0
                        || path.IndexOf("Trailer", StringComparison.OrdinalIgnoreCase) >= 0
                        || path.IndexOf("Sedan", StringComparison.OrdinalIgnoreCase) >= 0
                        || path.IndexOf("Pickup", StringComparison.OrdinalIgnoreCase) >= 0
                        || path.IndexOf("Wreck", StringComparison.OrdinalIgnoreCase) >= 0
                        || path.IndexOf("Van", StringComparison.OrdinalIgnoreCase) >= 0
                        || path.IndexOf("HayCart", StringComparison.OrdinalIgnoreCase) >= 0;
                })
                .Take(10)
                .ToList();
            if (vehicleSample.Count > 0)
            {
                MelonLogger.Msg($"vehicleIsh sample ({vehicleSample.Count}):");
                foreach (LeakProbeEntry e in vehicleSample)
                {
                    MelonLogger.Msg($"  [{e.Count}] class={e.ProposedClass} {e.Path}");
                }
            }
        }
        catch (Exception ex)
        {
            MelonLogger.Error($"dump_enrichment_names failed: {ex}");
            try { Il2Cpp.uConsoleLog.Add($"TerrainDumper ERROR: {ex.Message}"); } catch { /* ignore */ }
        }
    }

    public static void Tick()
    {
        if (!_running || _heights == null || _classes == null)
            return;

        try
        {
            EnsureHitBuffer();
            int end = Math.Min(_row + _rowsPerFrame, _height);
            for (int iz = _row; iz < end; iz++)
            {
                for (int ix = 0; ix < _width; ix++)
                {
                    float x = _originX + ix * _cellSize;
                    float z = _originZ + iz * _cellSize;
                    var origin = new Vector3(x, _rayStartY, z);
                    _rays++;
                    if (TrySampleHeight(origin, out float y, out Collider? hitCol))
                    {
                        int i = iz * _width + ix;
                        _heights[i] = y;
                        _classes[i] = (byte)ClassifyHitCached(hitCol);
                        _hits++;
                    }
                }
            }

            _row = end;
            if (_row % 50 == 0 || _row >= _height)
            {
                float pct = 100f * _row / Math.Max(1, _height);
                MelonLogger.Msg(
                    $"Enrichment raycast {_row}/{_height} rows ({pct:F0}%), hits={_hits}, skippedInvisible={_skippedInvisible}");
            }

            if (_row >= _height)
                Finish();
        }
        catch (Exception ex)
        {
            MelonLogger.Error($"Enrichment tick failed: {ex}");
            Abort();
        }
    }

    private static bool TrySampleHeight(Vector3 origin, out float y, out Collider? hitCol)
    {
        y = 0f;
        hitCol = null;
        // Fast path: one Raycast — almost every cell hits terrain first.
        if (!Physics.Raycast(
                origin, Vector3.down, out RaycastHit first, _rayDist,
                _raycastMask, QueryTriggerInteraction.Ignore))
            return false;

        Collider? firstCol = first.collider;
        // Terrain is never a wall — skip cache/dictionary entirely (majority of cells).
        if (firstCol != null && firstCol.TryCast<TerrainCollider>() != null)
        {
            y = first.point.y;
            hitCol = firstCol;
            return true;
        }

        if (!IsInvisibleWallColliderCached(firstCol))
        {
            y = first.point.y;
            hitCol = firstCol;
            return true;
        }

        _skippedInvisible++;
        int firstId = firstCol != null ? firstCol.GetInstanceID() : 0;
        // Slow path only when the nearest hit is a wall: gather all hits, keep nearest real geometry.
        EnsureHitBuffer();
        int n = Physics.RaycastNonAlloc(
            origin, Vector3.down, _hitBuf, _rayDist,
            _raycastMask, QueryTriggerInteraction.Ignore);
        if (n <= 0)
            return false;

        float bestDist = float.PositiveInfinity;
        float bestY = 0f;
        Collider? bestCol = null;
        bool found = false;
        int lim = Math.Min(n, HitBufferSize);
        for (int i = 0; i < lim; i++)
        {
            RaycastHit hit = _hitBuf![i];
            Collider? col = hit.collider;
            if (col == null)
                continue;
            if (col.TryCast<TerrainCollider>() != null)
            {
                if (hit.distance < bestDist)
                {
                    bestDist = hit.distance;
                    bestY = hit.point.y;
                    bestCol = col;
                    found = true;
                }
                continue;
            }
            if (IsInvisibleWallColliderCached(col))
            {
                if (col.GetInstanceID() != firstId)
                    _skippedInvisible++;
                continue;
            }
            if (hit.distance < bestDist)
            {
                bestDist = hit.distance;
                bestY = hit.point.y;
                bestCol = col;
                found = true;
            }
        }

        if (!found)
            return false;
        y = bestY;
        hitCol = bestCol;
        return true;
    }

    private static bool IsInvisibleWallColliderCached(Collider? col)
    {
        if (col == null)
            return true;
        if (col.TryCast<TerrainCollider>() != null)
            return false;

        _wallCache ??= new Dictionary<int, bool>(256);
        int id = col.GetInstanceID();
        if (_wallCache.TryGetValue(id, out bool wall))
            return wall;
        wall = IsInvisibleWallCollider(col);
        _wallCache[id] = wall;
        return wall;
    }

    private static void ClearWallCache()
    {
        _wallCache?.Clear();
        _classCache?.Clear();
    }

    private static void ResolveBarrierLayers()
    {
        _layerCharControllerOnly = LayerMask.NameToLayer("CharacterControllerCollideOnly");
        _layerNoCollidePlayer = LayerMask.NameToLayer("NoCollidePlayer");
        _layerParticleKiller = LayerMask.NameToLayer("ParticleKiller");
        _layerPlayer = LayerMask.NameToLayer("Player");
        _layerTriggerReverb = LayerMask.NameToLayer("TriggerReverb");
        _layerTriggerIgnoreRaycast = LayerMask.NameToLayer("TriggerIgnoreRaycast");

        // Exclude known lid layers from the raycast mask so they never become the first hit
        // (avoids millions of slow NonAlloc punch-throughs over WindSpeedTriggers boxes).
        int mask = Physics.DefaultRaycastLayers;
        void Exclude(int layer)
        {
            if (layer >= 0)
                mask &= ~(1 << layer);
        }
        Exclude(_layerCharControllerOnly);
        Exclude(_layerNoCollidePlayer);
        Exclude(_layerParticleKiller);
        Exclude(_layerPlayer);
        Exclude(_layerTriggerReverb);
        Exclude(_layerTriggerIgnoreRaycast);
        _raycastMask = mask;
    }

    /// <summary>
    /// Hinterland invisible barriers / story volumes: CharacterControllerCollideOnly under
    /// "COLLISION GROUP- HIDDEN", Player-layer prologue boxes, reverb spheres, etc.
    /// Does NOT use renderer heuristics (those false-positive tree logs / props).
    /// </summary>
    internal static bool IsInvisibleWallCollider(Collider col)
    {
        if (col == null)
            return true;
        if (col.TryCast<TerrainCollider>() != null)
            return false;

        GameObject? go = col.gameObject;
        if (go == null)
            return false;

        if (_layerCharControllerOnly == -2)
            ResolveBarrierLayers();

        int layer = go.layer;
        if (layer == _layerCharControllerOnly ||
            layer == _layerNoCollidePlayer ||
            layer == _layerParticleKiller ||
            layer == _layerPlayer ||
            layer == _layerTriggerReverb ||
            layer == _layerTriggerIgnoreRaycast)
            return true;

        // Walk parents for known barrier folder names (no full path string alloc).
        Transform? t = go.transform;
        for (int depth = 0; t != null && depth < 12; depth++)
        {
            string n = t.name;
            if (n.IndexOf("COLLISION GROUP", StringComparison.OrdinalIgnoreCase) >= 0)
                return true;
            if (n.IndexOf("InvivibleCollision", StringComparison.OrdinalIgnoreCase) >= 0)
                return true;
            if (n.IndexOf("InvisibleCollision", StringComparison.OrdinalIgnoreCase) >= 0)
                return true;
            // Story/prologue volumes (Keeper's Pass squares, etc.) — note Hinterland typo "Collsion".
            if (n.IndexOf("Prologue Collision", StringComparison.OrdinalIgnoreCase) >= 0)
                return true;
            if (n.IndexOf("Collsion", StringComparison.OrdinalIgnoreCase) >= 0)
                return true;
            // Cannery (and others): huge WindSpeedTriggers boxes (MaximumBoost / MinorBoost)
            // sit at ~Y=120 and flatten enrichment plateaus over real terrain.
            if (n.IndexOf("WindSpeedTriggers", StringComparison.OrdinalIgnoreCase) >= 0)
                return true;
            t = t.parent;
        }

        return false;
    }

    private static HitClass ClassifyHitCached(Collider? col)
    {
        if (col == null)
            return HitClass.None;
        _classCache ??= new Dictionary<int, byte>(256);
        int id = col.GetInstanceID();
        if (_classCache.TryGetValue(id, out byte cached))
            return (HitClass)cached;
        HitClass c = ClassifyHit(col);
        _classCache[id] = (byte)c;
        return c;
    }

    /// <summary>
    /// Map an accepted enrichment hit to a small class for offline contour/rim decisions.
    /// Patterns are intentionally conservative; refine after dump_enrichment_leak reviews.
    /// </summary>
    internal static HitClass ClassifyHit(Collider col)
    {
        if (col == null)
            return HitClass.None;
        if (col.TryCast<TerrainCollider>() != null)
            return HitClass.Terrain;

        GameObject? go = col.gameObject;
        if (go == null)
            return HitClass.Ignore;

        HitClass fromName = ClassifyFromNames(go.transform);
        if (fromName != HitClass.None)
            return fromName;

        // Unmatched primitive proxies are the usual square-plateau leaks — keep height, no overlay.
        if (IsPrimitiveProxyCollider(col))
            return HitClass.Ignore;

        return HitClass.Terrain;
    }

    private static HitClass ClassifyFromNames(Transform? start)
    {
        Transform? t = start;
        for (int depth = 0; t != null && depth < 12; depth++)
        {
            string n = t.name ?? "";
            // Structures before rock (e.g. "RockBridge" → structure).
            if (NameLooksLikeStructure(n))
                return HitClass.Structure;
            if (n.IndexOf("Rock", StringComparison.OrdinalIgnoreCase) >= 0
                || n.IndexOf("Boulder", StringComparison.OrdinalIgnoreCase) >= 0
                || n.IndexOf("Cliff", StringComparison.OrdinalIgnoreCase) >= 0)
                return HitClass.Rock;
            // Match OrthoDump / mapalign backdrop tokens, plus shelf/backdrop lids.
            if (n.IndexOf("Ice", StringComparison.OrdinalIgnoreCase) >= 0
                || n.IndexOf("Water", StringComparison.OrdinalIgnoreCase) >= 0
                || n.IndexOf("Pond", StringComparison.OrdinalIgnoreCase) >= 0
                || n.IndexOf("Creek", StringComparison.OrdinalIgnoreCase) >= 0
                || n.IndexOf("Shelf", StringComparison.OrdinalIgnoreCase) >= 0
                || n.IndexOf("Backdrop", StringComparison.OrdinalIgnoreCase) >= 0)
                return HitClass.IceBackdrop;
            t = t.parent;
        }

        return HitClass.None;
    }

    private static bool NameLooksLikeStructure(string n)
    {
        // Man-made props / buildings. Prefer specific tokens (avoid bare "Camp" → Campfire).
        // Verified from enrichment_names.json + DetailedMaps POI stems.
        foreach (string token in StructureNameTokens)
        {
            if (StructureTokenMatches(n, token))
                return true;
        }
        return false;
    }

    /// <summary>
    /// Substring match with guards against known false positives
    /// (Dam⊂Damaged, Bus⊂Bush, Hall⊂Shallow).
    /// </summary>
    private static bool StructureTokenMatches(string n, string token)
    {
        int start = 0;
        while (true)
        {
            int idx = n.IndexOf(token, start, StringComparison.OrdinalIgnoreCase);
            if (idx < 0)
                return false;

            // "Dam" must not match "Damaged" / "Damage".
            if (token.Equals("Dam", StringComparison.OrdinalIgnoreCase))
            {
                if (n.IndexOf("Damag", StringComparison.OrdinalIgnoreCase) >= 0)
                    return false;
                return true;
            }

            // "Bus" must not match "Bush".
            if (token.Equals("Bus", StringComparison.OrdinalIgnoreCase))
            {
                int after = idx + token.Length;
                if (after < n.Length && (n[after] == 'h' || n[after] == 'H'))
                {
                    start = idx + 1;
                    continue;
                }
                return true;
            }

            // "Hall" must not match the letters inside "Shallow" (CaveShallowA false structure).
            if (token.Equals("Hall", StringComparison.OrdinalIgnoreCase))
            {
                if (IsSpanInsideToken(n, idx, token.Length, "Shallow"))
                {
                    start = idx + 1;
                    continue;
                }
                return true;
            }

            return true;
        }
    }

    /// <summary>True when [idx, idx+len) lies inside some occurrence of <paramref name="container"/>.</summary>
    private static bool IsSpanInsideToken(string n, int idx, int len, string container)
    {
        int search = 0;
        while (true)
        {
            int s = n.IndexOf(container, search, StringComparison.OrdinalIgnoreCase);
            if (s < 0)
                return false;
            if (idx >= s && idx + len <= s + container.Length)
                return true;
            search = s + 1;
        }
    }

    private static readonly string[] StructureNameTokens =
    {
        // Waterfront / crossings
        "Bridge", "Dock", "Pier", "Wharf", "Jetty", "Boardwalk", "Boathouse",
        "Walkway", "Stairs", "Ladder", "Platform", "Ramp", "Deck",
        "Boat", "Ship", "Speeder", "OceanPost", "FishingNet", "Slipway",
        // Buildings / settlement (Milton + elsewhere)
        "Building", "Cabin", "House", "Cottage", "Barn", "Shed", "Hut", "Garage",
        "Workshop", "Hangar", "Quonset", "Office", "Church", "School", "Bank", "Store",
        "Farmhouse", "Farm", "Homestead", "Lodge", "Hall", "Prison",
        "Lighthouse", "Lookout", "Tower", "Substation", "Station",
        "Trailer", "HuntingLodge", "CommunityHall", "CampOffice", "Campground",
        "Trapper", "Bricklayer", "StoneHut", "StoneCabin", "Shelter", "Blind",
        "Gate", "Powerplant", "PowerPlant",
        // Industrial / region landmarks
        "Dam", "HydroDam", "CarterHydro", "Cannery", "Whaling", "Hibernia", "Riken",
        "Mine", "Bunker", "Hatch", "Prepper", "Elevator",
        "Concentrator", "Headframe", "Pumphouse", "MaintenanceYard", "Derailment",
        "RadioTower", "Radio", "Antenna", "Beacon", "ControlTower",
        "Runway", "OilDerrick", "Derrick", "OilTank", "WhaleOilTank",
        "CemmentBarrier", "PipeJunk", "MetalBarrel", "MetalPipe", "PalletPile",
        "IndustrialDebris", "WoodPlanks", "BrickPile", "FloodLight",
        // Fences / site dressing (prefer specific stems over bare "Fence")
        "FenceWood", "FenceSecurity", "FenceWire", "HayBale", "StoneWall",
        "PoleWood", "SignPark", "PicnicTable", "PlantSupport", "BarrelWood",
        // Vehicles / rail / cargo — CarSedan covers undamaged + Damaged sedan prefabs (names probe)
        "CarSedan", "CarTruck", "MineTruck", "HayCart", "CargoContainer",
        "Plane", "Aircraft", "Wreck", "Truck", "Train", "Rail", "Bus", "Helicopter",
        "Locomotive",
        // Wood stacks / fallen timber props
        "Woodpile", "LogPile", "LogFallen", "FallenLog", "OBJ_Log", "TRN_Log",
        // Common prefab stems for man-made art (STRSPAWN_ houses are not "STR_")
        "STRSPAWN", "STR_", "BLD_",
    };

    private static void EnsureHitBuffer()
    {
        _hitBuf ??= new Il2CppStructArray<RaycastHit>(HitBufferSize);
    }

    private static void Begin(float cellSize, int rowsPerFrame = DefaultRowsPerFrame)
    {
        ComputeTerrainBounds(out float minX, out float maxX, out float minZ, out float maxZ, out float minY, out float maxY);

        _sceneName = UnitySceneManager.GetActiveScene().name;
        if (string.IsNullOrWhiteSpace(_sceneName))
            _sceneName = "UnknownScene";

        _outDir = Path.Combine(MelonEnvironment.ModsDirectory, "TerrainDumper", _sceneName);
        Directory.CreateDirectory(_outDir);

        _cellSize = cellSize;
        _rowsPerFrame = rowsPerFrame;
        _originX = minX;
        _originZ = minZ;
        _minY = minY;
        _maxY = maxY;
        _width = Math.Max(2, Mathf.FloorToInt((maxX - minX) / _cellSize) + 1);
        _height = Math.Max(2, Mathf.FloorToInt((maxZ - minZ) / _cellSize) + 1);
        _heights = new float[_width * _height];
        Array.Fill(_heights, float.NaN);
        _classes = new byte[_width * _height];
        _row = 0;
        _hits = 0;
        _rays = 0;
        _skippedInvisible = 0;
        _rayStartY = maxY + 250f;
        _rayDist = (maxY - minY) + 500f;
        EnsureHitBuffer();
        ClearWallCache();
        ResolveBarrierLayers();
        _running = true;
        Implementation.EnrichmentTicksEnabled = true;
        DumpBackground.Acquire();

        Msg(
            $"Enrichment started: {_width}x{_height} @ {_cellSize}m " +
            $"({_width * _height} rays), rowsPerFrame={_rowsPerFrame} " +
            $"over X[{minX:F0},{maxX:F0}] Z[{minZ:F0},{maxZ:F0}] " +
            "(skipping invisible walls; labeling hit classes). This takes a bit…");
    }

    private static void ComputeTerrainBounds(
        out float minX, out float maxX, out float minZ, out float maxZ, out float minY, out float maxY)
    {
        Terrain[] terrains = Terrain.activeTerrains;
        if (terrains == null || terrains.Length == 0)
            throw new InvalidOperationException("No active Terrains — load a region first.");

        minX = float.PositiveInfinity;
        maxX = float.NegativeInfinity;
        minZ = float.PositiveInfinity;
        maxZ = float.NegativeInfinity;
        minY = float.PositiveInfinity;
        maxY = float.NegativeInfinity;

        foreach (Terrain t in terrains)
        {
            if (t == null || t.terrainData == null)
                continue;
            Vector3 pos = t.transform.position;
            // Unity Terrain ignores its Transform scale, so terrainData.size *is* the world
            // extent. TLD ships scaled water terrains (Terrain_CoastalWater at 326x) that would
            // otherwise blow the grid up to millions of metres.
            Vector3 size = t.terrainData.size;
            float x0 = pos.x;
            float z0 = pos.z;
            float x1 = pos.x + size.x;
            float z1 = pos.z + size.z;
            float y0 = pos.y;
            float y1 = pos.y + size.y;
            if (x0 < minX) minX = x0;
            if (x1 > maxX) maxX = x1;
            if (z0 < minZ) minZ = z0;
            if (z1 > maxZ) maxZ = z1;
            if (y0 < minY) minY = y0;
            if (y1 > maxY) maxY = y1;
        }

        if (!float.IsFinite(minX))
            throw new InvalidOperationException("Could not compute terrain bounds (all active Terrains were scaled backdrops).");
    }

    private static void Abort()
    {
        if (!_running)
            return;
        _running = false;
        Implementation.EnrichmentTicksEnabled = false;
        _heights = null;
        _classes = null;
        DumpBackground.Release();
    }

    private static void WriteNamesJson(
        string outDir,
        string sceneName,
        string fileName,
        float cellSize,
        float? minAboveMeters,
        int width,
        int height,
        int rays,
        int namedRays,
        IReadOnlyList<LeakProbeEntry> list,
        string rule)
    {
        var byClass = list
            .GroupBy(e => e.ProposedClass)
            .OrderByDescending(g => g.Sum(x => x.Count))
            .Select(g => new { @class = g.Key, colliders = g.Count(), cells = g.Sum(x => x.Count) })
            .ToList();

        static bool LooksVehicleIsh(string path)
        {
            if (path.IndexOf("Carcass", StringComparison.OrdinalIgnoreCase) >= 0)
                return false;
            return path.IndexOf("Car", StringComparison.OrdinalIgnoreCase) >= 0
                || path.IndexOf("Vehicle", StringComparison.OrdinalIgnoreCase) >= 0
                || path.IndexOf("Truck", StringComparison.OrdinalIgnoreCase) >= 0
                || path.IndexOf("Bus", StringComparison.OrdinalIgnoreCase) >= 0
                || path.IndexOf("Trailer", StringComparison.OrdinalIgnoreCase) >= 0
                || path.IndexOf("Sedan", StringComparison.OrdinalIgnoreCase) >= 0
                || path.IndexOf("Pickup", StringComparison.OrdinalIgnoreCase) >= 0
                || path.IndexOf("Wreck", StringComparison.OrdinalIgnoreCase) >= 0
                || path.IndexOf("Van", StringComparison.OrdinalIgnoreCase) >= 0
                || path.IndexOf("HayCart", StringComparison.OrdinalIgnoreCase) >= 0;
        }

        var vehicleIsh = list.Where(e => LooksVehicleIsh(e.Path)).ToList();

        object EntryPayload(LeakProbeEntry e) => new
        {
            e.Name,
            e.Path,
            e.ColliderType,
            e.Layer,
            e.LayerName,
            e.HasRenderer,
            e.ProposedClass,
            size = new { x = e.SizeX, y = e.SizeY, z = e.SizeZ },
            e.Count,
            maxAboveMeters = e.MaxAboveMeters,
            example = new
            {
                x = e.ExampleX,
                y = e.ExampleY,
                z = e.ExampleZ,
                groundY = float.IsFinite(e.ExampleGroundY) ? e.ExampleGroundY : (float?)null,
            },
        };

        var payload = new
        {
            sceneName,
            dumpedAtUtc = DateTime.UtcNow.ToString("o", CultureInfo.InvariantCulture),
            modVersion = Implementation.ModVersion,
            cellSize,
            minAboveMeters,
            width,
            height,
            rays,
            namedRays,
            uniqueColliders = list.Count,
            rule,
            skipRule = SkipRuleDescription,
            classRule = ClassRuleDescription,
            proposedClassSummary = byClass,
            vehicleIshCount = vehicleIsh.Count,
            vehicleIsh = vehicleIsh.Take(100).Select(EntryPayload),
            // Full unique list (no 500 cap) so rare props stay visible.
            entries = list.Select(EntryPayload),
        };

        string path = Path.Combine(outDir, fileName);
        File.WriteAllText(
            path,
            JsonSerializer.Serialize(payload, JsonOpts),
            new UTF8Encoding(encoderShouldEmitUTF8Identifier: false));
    }

    private static void Finish()
    {
        if (_heights == null || _classes == null)
        {
            Abort();
            return;
        }

        float hMin = float.PositiveInfinity;
        float hMax = float.NegativeInfinity;
        for (int i = 0; i < _heights.Length; i++)
        {
            float h = _heights[i];
            if (float.IsNaN(h))
                continue;
            if (h < hMin) hMin = h;
            if (h > hMax) hMax = h;
        }

        if (!float.IsFinite(hMin))
        {
            hMin = _minY;
            hMax = _maxY;
        }

        // Avoid zero span
        if (hMax - hMin < 0.01f)
            hMax = hMin + 1f;

        var mask = new byte[_width * _height];
        var raw = new byte[_width * _height * 2];
        int o = 0;
        for (int i = 0; i < _heights.Length; i++)
        {
            float h = _heights[i];
            if (float.IsNaN(h))
            {
                mask[i] = 0;
                raw[o++] = 0;
                raw[o++] = 0;
            }
            else
            {
                mask[i] = 1;
                float n = Mathf.Clamp01((h - hMin) / (hMax - hMin));
                ushort q = (ushort)Mathf.RoundToInt(n * 65535f);
                raw[o++] = (byte)(q & 0xFF);
                raw[o++] = (byte)(q >> 8);
            }
        }

        string heightsName = "enrichment_heights.raw";
        string maskName = "enrichment_mask.raw";
        string className = "enrichment_class.raw";
        File.WriteAllBytes(Path.Combine(_outDir, heightsName), raw);
        File.WriteAllBytes(Path.Combine(_outDir, maskName), mask);
        File.WriteAllBytes(Path.Combine(_outDir, className), _classes);

        int[] classHist = new int[ClassLabelNames.Length];
        for (int i = 0; i < _classes.Length; i++)
        {
            int c = _classes[i];
            if (c >= 0 && c < classHist.Length)
                classHist[c]++;
        }

        var meta = new EnrichmentMeta
        {
            FormatVersion = 3,
            SceneName = _sceneName,
            DumpedAtUtc = DateTime.UtcNow.ToString("o", CultureInfo.InvariantCulture),
            ModVersion = Implementation.ModVersion,
            Method =
                "Physics.Raycast down (NonAlloc only if top hit is barrier); " +
                "skip " + SkipRuleDescription + "; " +
                "classify " + ClassRuleDescription + "; " +
                "raycast mask = DefaultRaycastLayers minus barrier/TriggerIgnoreRaycast layers; Ignore triggers",
            CellSize = _cellSize,
            Width = _width,
            Height = _height,
            OriginX = _originX,
            OriginZ = _originZ,
            SizeX = (_width - 1) * _cellSize,
            SizeZ = (_height - 1) * _cellSize,
            HeightMinMeters = hMin,
            HeightMaxMeters = hMax,
            HitCount = _hits,
            RayCount = _rays,
            SkippedInvisibleCount = _skippedInvisible,
            HeightsFile = heightsName,
            MaskFile = maskName,
            ClassFile = className,
            ClassLabels = ClassLabelNames,
            Notes = new[]
            {
                "Row-major [z, x]; world X = originX + ix*cellSize, Z = originZ + iz*cellSize.",
                "uint16 = round((meters - heightMin) / (heightMax - heightMin) * 65535); mask 0 = no hit.",
                "enrichment_class.raw: 1 byte/cell HitClass id (see classLabels); none=0 when mask=0.",
                "Merge with terrain DEM via max() in make_map_bg.py; contour-suppress rock+structure; " +
                "rock = soft rim, structure (bridges/docks/buildings/logs) = thin sharp rim.",
                "Invisible barriers / Player prologue volumes / TriggerReverb / WindSpeedTriggers are skipped; next hit down is kept.",
            },
        };

        File.WriteAllText(
            Path.Combine(_outDir, "enrichment_meta.json"),
            JsonSerializer.Serialize(meta, JsonOpts),
            new UTF8Encoding(encoderShouldEmitUTF8Identifier: false));

        var histBits = new List<string>(ClassLabelNames.Length);
        for (int i = 0; i < ClassLabelNames.Length; i++)
        {
            if (classHist[i] > 0)
                histBits.Add($"{ClassLabelNames[i]}={classHist[i]}");
        }

        Msg(
            $"Enrichment done → {_outDir} (hits={_hits}/{_rays}, skippedInvisible={_skippedInvisible}, " +
            $"Y={hMin:F1}..{hMax:F1}m, classes: {string.Join(" ", histBits)})");
        _running = false;
        Implementation.EnrichmentTicksEnabled = false;
        _heights = null;
        _classes = null;
        DumpBackground.Release();
    }

    private static bool IsPrimitiveProxyCollider(Collider col)
    {
        return col.TryCast<BoxCollider>() != null
            || col.TryCast<CapsuleCollider>() != null
            || col.TryCast<SphereCollider>() != null;
    }

    private static void GetColliderApproxSize(Collider col, out float sx, out float sy, out float sz)
    {
        sx = sy = sz = 0f;
        Vector3 lossy = col.transform.lossyScale;
        BoxCollider? box = col.TryCast<BoxCollider>();
        if (box != null)
        {
            Vector3 s = box.size;
            sx = Mathf.Abs(s.x * lossy.x);
            sy = Mathf.Abs(s.y * lossy.y);
            sz = Mathf.Abs(s.z * lossy.z);
            return;
        }
        CapsuleCollider? cap = col.TryCast<CapsuleCollider>();
        if (cap != null)
        {
            float r = Mathf.Abs(cap.radius);
            float height = Mathf.Abs(cap.height);
            // Capsule radius is in X/Z of the capsule axis local space; approximate world extents.
            float maxScale = Mathf.Max(Mathf.Abs(lossy.x), Mathf.Abs(lossy.z));
            float yScale = Mathf.Abs(lossy.y);
            sx = sz = r * 2f * maxScale;
            sy = Mathf.Max(height * yScale, sx);
            return;
        }
        SphereCollider? sph = col.TryCast<SphereCollider>();
        if (sph != null)
        {
            float d = Mathf.Abs(sph.radius) * 2f * Mathf.Max(Mathf.Abs(lossy.x), Mathf.Max(Mathf.Abs(lossy.y), Mathf.Abs(lossy.z)));
            sx = sy = sz = d;
        }
    }

    /// <summary>
    /// Cheap renderer presence check (self → parents, then direct children).
    /// Used by leak probe only — not the enrichment skip rule.
    /// </summary>
    private static bool HasVisualRenderer(GameObject? go)
    {
        if (go == null)
            return false;
        Transform? t = go.transform;
        for (int depth = 0; t != null && depth < 12; depth++)
        {
            GameObject g = t.gameObject;
            if (g.GetComponent<MeshRenderer>() != null)
                return true;
            if (g.GetComponent<SkinnedMeshRenderer>() != null)
                return true;
            if (g.GetComponent<Terrain>() != null)
                return true;
            t = t.parent;
        }
        Transform root = go.transform;
        int n = Mathf.Min(root.childCount, 48);
        for (int i = 0; i < n; i++)
        {
            Transform c = root.GetChild(i);
            if (c.GetComponent<MeshRenderer>() != null)
                return true;
            if (c.GetComponent<SkinnedMeshRenderer>() != null)
                return true;
        }
        return false;
    }

    private static string ColliderTypeName(Collider col)
    {
        if (col.TryCast<BoxCollider>() != null) return "BoxCollider";
        if (col.TryCast<CapsuleCollider>() != null) return "CapsuleCollider";
        if (col.TryCast<MeshCollider>() != null) return "MeshCollider";
        if (col.TryCast<TerrainCollider>() != null) return "TerrainCollider";
        if (col.TryCast<SphereCollider>() != null) return "SphereCollider";
        return col.GetType().Name;
    }

    private static string ProbeKey(Collider col)
    {
        string path = GameObjectPath(col.gameObject);
        return ColliderTypeName(col) + "|" + path;
    }

    private static string GameObjectPath(GameObject? go)
    {
        if (go == null)
            return "(null)";
        var parts = new List<string>(8);
        Transform? t = go.transform;
        int guard = 0;
        while (t != null && guard++ < 32)
        {
            parts.Add(t.name);
            t = t.parent;
        }
        parts.Reverse();
        return string.Join("/", parts);
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

    private sealed class ProbeEntry
    {
        public string Key { get; set; } = "";
        public string Name { get; set; } = "";
        public string Path { get; set; } = "";
        public string ColliderType { get; set; } = "";
        public int Layer { get; set; }
        public string LayerName { get; set; } = "";
        public int Count { get; set; }
        public float ExampleX { get; set; }
        public float ExampleY { get; set; }
        public float ExampleZ { get; set; }
    }

    private sealed class LeakProbeEntry
    {
        public string Key { get; set; } = "";
        public string Name { get; set; } = "";
        public string Path { get; set; } = "";
        public string ColliderType { get; set; } = "";
        public int Layer { get; set; }
        public string LayerName { get; set; } = "";
        public bool HasRenderer { get; set; }
        public string ProposedClass { get; set; } = "";
        public float SizeX { get; set; }
        public float SizeY { get; set; }
        public float SizeZ { get; set; }
        public int Count { get; set; }
        public float MaxAboveMeters { get; set; }
        public float ExampleX { get; set; }
        public float ExampleY { get; set; }
        public float ExampleZ { get; set; }
        public float ExampleGroundY { get; set; }
    }

    private sealed class EnrichmentMeta
    {
        [JsonPropertyName("formatVersion")] public int FormatVersion { get; set; }
        [JsonPropertyName("sceneName")] public string SceneName { get; set; } = "";
        [JsonPropertyName("dumpedAtUtc")] public string DumpedAtUtc { get; set; } = "";
        [JsonPropertyName("modVersion")] public string ModVersion { get; set; } = "";
        [JsonPropertyName("method")] public string Method { get; set; } = "";
        [JsonPropertyName("cellSize")] public float CellSize { get; set; }
        [JsonPropertyName("width")] public int Width { get; set; }
        [JsonPropertyName("height")] public int Height { get; set; }
        [JsonPropertyName("originX")] public float OriginX { get; set; }
        [JsonPropertyName("originZ")] public float OriginZ { get; set; }
        [JsonPropertyName("sizeX")] public float SizeX { get; set; }
        [JsonPropertyName("sizeZ")] public float SizeZ { get; set; }
        [JsonPropertyName("heightMinMeters")] public float HeightMinMeters { get; set; }
        [JsonPropertyName("heightMaxMeters")] public float HeightMaxMeters { get; set; }
        [JsonPropertyName("hitCount")] public int HitCount { get; set; }
        [JsonPropertyName("rayCount")] public int RayCount { get; set; }
        [JsonPropertyName("skippedInvisibleCount")] public int SkippedInvisibleCount { get; set; }
        [JsonPropertyName("heightsFile")] public string HeightsFile { get; set; } = "";
        [JsonPropertyName("maskFile")] public string MaskFile { get; set; } = "";
        [JsonPropertyName("classFile")] public string? ClassFile { get; set; }
        [JsonPropertyName("classLabels")] public string[]? ClassLabels { get; set; }
        [JsonPropertyName("notes")] public string[]? Notes { get; set; }
    }
}
