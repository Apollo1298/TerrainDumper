using System.Collections;
using System.Globalization;
using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;
using System.Text.RegularExpressions;
using Il2Cpp;
using Il2CppSuperSplines;
using MelonLoader;
using MelonLoader.Utils;
using UnityEngine;
using UnitySceneManager = UnityEngine.SceneManagement.SceneManager;

namespace TerrainDumper;

/// <summary>
/// Dump rope climbs (RopeAnchorPoint / Rope) and scramble climbs (Cliff_Climbable*) with length.
/// Standalone — not part of dump_map. Batch: dump_climbing_all walks regions via scene loads.
/// </summary>
internal static class ClimbingDump
{
    private const int FormatVersion = 1;
    private static readonly Regex MetersInName = new(@"(\d+)\s*m\b", RegexOptions.IgnoreCase | RegexOptions.CultureInvariant);

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

    private static bool _batchRunning;
    private static bool _batchAbort;

    public static bool IsBatchRunning => _batchRunning;

    public static void RunFromConsole() => Run();

    public static void Run()
    {
        try
        {
            Dump();
        }
        catch (Exception ex)
        {
            MelonLogger.Error($"dump_climbing failed: {ex}");
            try { uConsoleLog.Add($"TerrainDumper ERROR: {ex.Message}"); } catch { /* ignore */ }
        }
    }

    /// <summary>
    /// Load each region (same path as console <c>scene</c>), wait until ready, dump_climbing, next.
    /// Optional args: scene names; omit for the default vanilla outdoor list.
    /// </summary>
    public static void RunAllFromConsole() => StartBatchFromConsole(DefaultBatchScenes, "dump_climbing_all");

    /// <summary>
    /// Same as <see cref="RunAllFromConsole"/> but defaults to Mod* outdoor scenes.
    /// Optional args override with an explicit scene list.
    /// </summary>
    public static void RunAllModsFromConsole() => StartBatchFromConsole(DefaultModBatchScenes, "dump_climbing_all_mods");

    private static void StartBatchFromConsole(string[] defaults, string label)
    {
        if (_batchRunning)
        {
            Msg($"{label} already running — dump_climbing_all_abort to cancel.");
            return;
        }

        var scenes = new List<string>();
        try
        {
            int n = uConsole.GetNumParameters();
            for (int i = 0; i < n; i++)
            {
                string? s = null;
                try { s = uConsole.GetString(); } catch { /* ignore */ }
                if (!string.IsNullOrWhiteSpace(s))
                    scenes.Add(s.Trim());
            }
        }
        catch { /* use defaults */ }

        if (scenes.Count == 0)
            scenes.AddRange(defaults);

        try
        {
            MelonCoroutines.Start(CoDumpAll(scenes, label));
        }
        catch (Exception ex)
        {
            MelonLogger.Error($"{label} failed to start: {ex}");
            try { uConsoleLog.Add($"TerrainDumper ERROR: {ex.Message}"); } catch { /* ignore */ }
        }
    }

    public static void AbortAllFromConsole()
    {
        if (!_batchRunning)
        {
            Msg("dump_climbing_all is not running.");
            return;
        }

        _batchAbort = true;
        Msg("dump_climbing_all abort requested — finishing current scene then stopping.");
    }

    private static IEnumerator CoDumpAll(List<string> scenes, string label)
    {
        _batchRunning = true;
        _batchAbort = false;
        DumpBackground.Acquire();

        int ok = 0;
        int fail = 0;
        Msg($"{label} start — {scenes.Count} scene(s). Leave the game running (unfocused OK).");

        try
        {
            for (int i = 0; i < scenes.Count; i++)
            {
                if (_batchAbort)
                {
                    Msg($"{label} aborted after {ok} ok / {fail} fail.");
                    yield break;
                }

                string scene = scenes[i];
                Msg($"{label} [{i + 1}/{scenes.Count}] → {scene}");

                bool ready = false;
                yield return CoEnsureScene(scene, r => ready = r);
                DumpBackground.Pulse();
                if (!ready)
                {
                    fail++;
                    Msg($"{label} SKIP {scene} — load timed out or failed.");
                    continue;
                }

                try
                {
                    Dump();
                    ok++;
                }
                catch (Exception ex)
                {
                    fail++;
                    MelonLogger.Error($"{label} dump failed in {scene}: {ex}");
                    Msg($"{label} FAIL {scene}: {ex.Message}");
                }

                yield return null;
            }

            Msg($"{label} COMPLETE — ok={ok}, fail={fail}, total={scenes.Count}");
        }
        finally
        {
            DumpBackground.Release();
            _batchRunning = false;
            _batchAbort = false;
        }
    }

    private static IEnumerator CoEnsureScene(string scene, Action<bool> onDone)
    {
        if (SceneMatches(scene))
        {
            Msg($"Already in {scene} — settle then dump.");
            yield return CoSettleReady(scene);
            onDone(SceneMatches(scene));
            yield break;
        }

        if (!TryBeginSceneLoad(scene))
        {
            onDone(false);
            yield break;
        }

        float t0 = Time.realtimeSinceStartup;
        // Wait until active outdoor scene matches and async loads finish (GearSpawner / Fast_Travel init follow).
        while (Time.realtimeSinceStartup - t0 < SceneLoadTimeoutSec)
        {
            if (_batchAbort)
            {
                onDone(false);
                yield break;
            }

            if (SceneMatches(scene) && !IsSceneLoadBusy())
                break;

            DumpBackground.Pulse();
            yield return null;
        }

        if (!SceneMatches(scene) || IsSceneLoadBusy())
        {
            MelonLogger.Warning(
                $"dump_climbing_all: timed out waiting for '{scene}' (active='{CurrentSceneName()}', busy={IsSceneLoadBusy()})");
            onDone(false);
            yield break;
        }

        yield return CoSettleReady(scene);
        onDone(SceneMatches(scene));
    }

    /// <summary>
    /// Extra wait after load: mirrors log gap GearSpawner → Fast_Travel Scene initialized (+ additive *_WILDLIFE etc.).
    /// </summary>
    private static IEnumerator CoSettleReady(string scene)
    {
        float t0 = Time.realtimeSinceStartup;
        while (Time.realtimeSinceStartup - t0 < SceneSettleSec)
        {
            if (_batchAbort)
                yield break;
            // Prefer a bit of terrain present for outdoor regions; don't require it (caves/transition edge cases).
            DumpBackground.Pulse();
            yield return null;
        }

        // One more beat if additive async loads are still spinning.
        float t1 = Time.realtimeSinceStartup;
        while (IsSceneLoadBusy() && Time.realtimeSinceStartup - t1 < 30f)
        {
            if (_batchAbort)
                yield break;
            DumpBackground.Pulse();
            yield return null;
        }

        if (!SceneMatches(scene))
            MelonLogger.Warning($"dump_climbing_all: scene drifted during settle (want '{scene}', have '{CurrentSceneName()}')");
    }

    private static bool TryBeginSceneLoad(string scene)
    {
        try
        {
            GameManager.LoadSceneWithLoadingScreen(scene);
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
            Msg($"uConsole scene {scene}");
            return true;
        }
        catch (Exception ex)
        {
            MelonLogger.Error($"dump_climbing_all: could not start load for '{scene}': {ex}");
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

    private static void Dump()
    {
        string sceneName = UnitySceneManager.GetActiveScene().name;
        if (string.IsNullOrWhiteSpace(sceneName))
            sceneName = "UnknownScene";

        string outDir = Path.Combine(MelonEnvironment.ModsDirectory, "TerrainDumper", sceneName);
        Directory.CreateDirectory(outDir);

        var ropeClimbs = CollectRopeClimbs(out int ropeAnchorCount, out int ropeObjectCount);
        var scrambles = CollectScrambles(out int cliffRootCount);

        ropeClimbs.Sort((a, b) =>
        {
            int c = string.CompareOrdinal(a.GameObject ?? "", b.GameObject ?? "");
            if (c != 0) return c;
            return a.X.CompareTo(b.X);
        });
        scrambles.Sort((a, b) =>
        {
            int c = string.CompareOrdinal(a.GameObject ?? "", b.GameObject ?? "");
            if (c != 0) return c;
            return a.X.CompareTo(b.X);
        });

        Vector3? playerPos = null;
        try
        {
            Transform? pt = GameManager.GetPlayerTransform();
            if (pt != null)
                playerPos = pt.position;
        }
        catch { /* ignore */ }

        var doc = new ClimbingDoc
        {
            FormatVersion = FormatVersion,
            SceneName = sceneName,
            DumpedAtUtc = DateTime.UtcNow.ToString("o", CultureInfo.InvariantCulture),
            ModVersion = Implementation.ModVersion,
            Method = "FindObjectsOfType<RopeAnchorPoint/Rope> + Cliff_Climbable transform roots",
            RopeAnchorCount = ropeAnchorCount,
            RopeObjectCount = ropeObjectCount,
            RopeClimbCount = ropeClimbs.Count,
            CliffRootCount = cliffRootCount,
            ScrambleCount = scrambles.Count,
            PlayerX = playerPos?.x,
            PlayerY = playerPos?.y,
            PlayerZ = playerPos?.z,
            RopeClimbs = ropeClimbs,
            Scrambles = scrambles,
            Notes = new[]
            {
                "ropeClimbs[]: mountaineering rope sections (RopeAnchorPoint). lengthMeters from SuperSplines.Spline.Length (minus unused top/bottom) when possible, else name (_50m / _100m).",
                "scrambles[]: short rock climbs (Cliff_Climbable*). lengthMeters from INTERACT_CLIMB_ENABLED ↔ INTERACT_CLIMBDOWN_ENABLED span when possible, else name (_5m).",
                "x/y/z: world position of the anchor (rope) or climb root (scramble). top/bottom or climbUp/climbDown are endpoints when found.",
                "Deployed/snapped rope state and mid-climb ledges are intentionally omitted.",
            },
        };

        string path = Path.Combine(outDir, "climbing.json");
        File.WriteAllText(
            path,
            JsonSerializer.Serialize(doc, JsonOpts),
            new UTF8Encoding(encoderShouldEmitUTF8Identifier: false));

        Msg($"Climbing done → {path} (ropes={ropeClimbs.Count}, scrambles={scrambles.Count})");
    }

    private static List<RopeClimbEntry> CollectRopeClimbs(out int anchorCount, out int ropeCount)
    {
        var list = new List<RopeClimbEntry>();
        anchorCount = 0;
        ropeCount = 0;

        RopeAnchorPoint[]? anchors = null;
        try { anchors = UnityEngine.Object.FindObjectsOfType<RopeAnchorPoint>(true); }
        catch (Exception ex)
        {
            MelonLogger.Warning($"FindObjectsOfType<RopeAnchorPoint> failed: {ex.Message}");
        }

        Rope[]? ropes = null;
        try { ropes = UnityEngine.Object.FindObjectsOfType<Rope>(true); }
        catch (Exception ex)
        {
            MelonLogger.Warning($"FindObjectsOfType<Rope> failed: {ex.Message}");
        }
        ropeCount = ropes?.Length ?? 0;

        var climbByRope = IndexClimbPointsByRope();
        var exitByRope = IndexExitPointsByRope();
        var seenRopes = new HashSet<int>();

        if (anchors != null)
        {
            foreach (RopeAnchorPoint ap in anchors)
            {
                if (ap == null)
                    continue;
                anchorCount++;

                Vector3 pos;
                string goName = "";
                string? path;
                try
                {
                    Transform tr = ap.transform;
                    pos = tr.position;
                    goName = ap.gameObject != null ? ap.gameObject.name : "";
                    path = BuildPath(tr);
                }
                catch
                {
                    continue;
                }

                Rope? rope = null;
                try { rope = ap.m_Rope; } catch { /* ignore */ }

                int ropeId = 0;
                try { if (rope != null) ropeId = rope.GetInstanceID(); } catch { /* ignore */ }
                if (ropeId != 0)
                    seenRopes.Add(ropeId);

                string lengthSource = "unknown";
                float? length = TryRopeSplineLength(rope, out Vector3? top, out Vector3? bottom, out lengthSource);

                if (length == null)
                {
                    float? fromName = ParseMetersFromName(goName) ?? ParseMetersFromName(path);
                    if (fromName != null)
                    {
                        length = fromName;
                        lengthSource = "name";
                    }
                }

                // Prefer typed climb/exit markers when spline ends missing.
                if (ropeId != 0)
                {
                    if (climbByRope.TryGetValue(ropeId, out Vector3 climbPos) && top == null)
                        top = climbPos;
                    if (exitByRope.TryGetValue(ropeId, out Vector3 exitPos) && bottom == null)
                        bottom = exitPos;
                }

                // If still no length but we have endpoints, use Euclidean distance.
                if (length == null && top != null && bottom != null)
                {
                    length = Vector3.Distance(top.Value, bottom.Value);
                    lengthSource = "endpoints";
                }

                list.Add(new RopeClimbEntry
                {
                    Kind = "rope",
                    GameObject = string.IsNullOrWhiteSpace(goName) ? null : goName,
                    Path = path,
                    X = pos.x,
                    Y = pos.y,
                    Z = pos.z,
                    LengthMeters = length,
                    LengthSource = length == null ? null : lengthSource,
                    Top = top == null ? null : Vec(top.Value),
                    Bottom = bottom == null ? null : Vec(bottom.Value),
                });
            }
        }

        // Orphan ropes with no RopeAnchorPoint (unusual, but keep coverage).
        if (ropes != null)
        {
            foreach (Rope rope in ropes)
            {
                if (rope == null)
                    continue;
                int id;
                try { id = rope.GetInstanceID(); }
                catch { continue; }
                if (!seenRopes.Add(id))
                    continue;

                Vector3 pos;
                string goName = "";
                string? path;
                try
                {
                    Transform tr = rope.transform;
                    pos = tr.position;
                    goName = rope.gameObject != null ? rope.gameObject.name : "";
                    path = BuildPath(tr);
                }
                catch
                {
                    continue;
                }

                string lengthSource = "unknown";
                float? length = TryRopeSplineLength(rope, out Vector3? top, out Vector3? bottom, out lengthSource);
                if (length == null)
                {
                    float? fromName = ParseMetersFromName(goName) ?? ParseMetersFromName(path);
                    if (fromName != null)
                    {
                        length = fromName;
                        lengthSource = "name";
                    }
                }
                if (length == null && top != null && bottom != null)
                {
                    length = Vector3.Distance(top.Value, bottom.Value);
                    lengthSource = "endpoints";
                }

                list.Add(new RopeClimbEntry
                {
                    Kind = "rope",
                    GameObject = string.IsNullOrWhiteSpace(goName) ? null : goName,
                    Path = path,
                    X = pos.x,
                    Y = pos.y,
                    Z = pos.z,
                    LengthMeters = length,
                    LengthSource = length == null ? null : lengthSource,
                    Top = top == null ? null : Vec(top.Value),
                    Bottom = bottom == null ? null : Vec(bottom.Value),
                });
            }
        }

        return list;
    }

    private static List<ScrambleEntry> CollectScrambles(out int cliffRootCount)
    {
        var list = new List<ScrambleEntry>();
        cliffRootCount = 0;

        Transform[]? transforms = null;
        try { transforms = UnityEngine.Object.FindObjectsOfType<Transform>(true); }
        catch (Exception ex)
        {
            MelonLogger.Warning($"FindObjectsOfType<Transform> (scrambles) failed: {ex.Message}");
            return list;
        }

        if (transforms == null)
            return list;

        var roots = new List<Transform>();
        foreach (Transform tr in transforms)
        {
            if (tr == null)
                continue;
            string n = tr.name ?? "";
            if (!n.StartsWith("Cliff_Climbable", StringComparison.OrdinalIgnoreCase))
                continue;
            Transform? parent = null;
            try { parent = tr.parent; } catch { /* ignore */ }
            string parentName = "";
            try { parentName = parent != null ? (parent.name ?? "") : ""; } catch { /* ignore */ }
            // Keep outermost Cliff_Climbable* only (skip mesh children like Cliff_Climbable_5m_v2).
            if (parentName.StartsWith("Cliff_Climbable", StringComparison.OrdinalIgnoreCase))
                continue;
            roots.Add(tr);
        }

        cliffRootCount = roots.Count;
        foreach (Transform root in roots)
        {
            Vector3 pos;
            string goName;
            string? path;
            try
            {
                pos = root.position;
                goName = root.name ?? "";
                path = BuildPath(root);
            }
            catch
            {
                continue;
            }

            Vector3? climbUp = null;
            Vector3? climbDown = null;
            try
            {
                foreach (Transform child in root.GetComponentsInChildren<Transform>(true))
                {
                    if (child == null)
                        continue;
                    string cn = child.name ?? "";
                    if (string.Equals(cn, "INTERACT_CLIMB_ENABLED", StringComparison.OrdinalIgnoreCase))
                        climbUp = child.position;
                    else if (string.Equals(cn, "INTERACT_CLIMBDOWN_ENABLED", StringComparison.OrdinalIgnoreCase))
                        climbDown = child.position;
                }
            }
            catch { /* ignore */ }

            string lengthSource = "unknown";
            float? length = null;
            if (climbUp != null && climbDown != null)
            {
                length = Vector3.Distance(climbUp.Value, climbDown.Value);
                lengthSource = "interactSpan";
            }
            else
            {
                float? fromName = ParseMetersFromName(goName) ?? ParseMetersFromName(path);
                if (fromName != null)
                {
                    length = fromName;
                    lengthSource = "name";
                }
            }

            list.Add(new ScrambleEntry
            {
                Kind = "scramble",
                GameObject = string.IsNullOrWhiteSpace(goName) ? null : goName,
                Path = path,
                X = pos.x,
                Y = pos.y,
                Z = pos.z,
                LengthMeters = length,
                LengthSource = length == null ? null : lengthSource,
                ClimbUp = climbUp == null ? null : Vec(climbUp.Value),
                ClimbDown = climbDown == null ? null : Vec(climbDown.Value),
            });
        }

        return list;
    }

    private static Dictionary<int, Vector3> IndexClimbPointsByRope()
    {
        var map = new Dictionary<int, Vector3>();
        RopeClimbPoint[]? points = null;
        try { points = UnityEngine.Object.FindObjectsOfType<RopeClimbPoint>(true); }
        catch { return map; }
        if (points == null)
            return map;
        foreach (RopeClimbPoint cp in points)
        {
            if (cp == null)
                continue;
            try
            {
                Rope? rope = cp.m_Rope;
                if (rope == null)
                    continue;
                int id = rope.GetInstanceID();
                map[id] = cp.transform.position;
            }
            catch { /* ignore */ }
        }
        return map;
    }

    private static Dictionary<int, Vector3> IndexExitPointsByRope()
    {
        var map = new Dictionary<int, Vector3>();
        RopeExitPoint[]? points = null;
        try { points = UnityEngine.Object.FindObjectsOfType<RopeExitPoint>(true); }
        catch { return map; }
        if (points == null)
            return map;
        foreach (RopeExitPoint ep in points)
        {
            if (ep == null)
                continue;
            try
            {
                Rope? rope = ep.m_Rope;
                if (rope == null)
                    continue;
                int id = rope.GetInstanceID();
                Vector3 pos = ep.transform.position;
                try
                {
                    Transform? exit = ep.m_ExitPoint;
                    if (exit != null)
                        pos = exit.position;
                }
                catch { /* ignore */ }
                map[id] = pos;
            }
            catch { /* ignore */ }
        }
        return map;
    }

    private static float? TryRopeSplineLength(Rope? rope, out Vector3? top, out Vector3? bottom, out string lengthSource)
    {
        top = null;
        bottom = null;
        lengthSource = "unknown";
        if (rope == null)
            return null;

        Spline? spline = null;
        try { spline = rope.m_Spline; }
        catch { /* ignore */ }
        if (spline == null)
            return null;

        try
        {
            try
            {
                top = spline.GetPositionOnSpline(0f);
                bottom = spline.GetPositionOnSpline(1f);
            }
            catch { /* ignore endpoints */ }

            if (top != null && bottom != null && top.Value.y < bottom.Value.y)
                (top, bottom) = (bottom, top);

            float len = 0f;
            try { len = spline.Length; }
            catch { /* ignore */ }

            float unusedTop = 0f;
            float unusedBottom = 0f;
            try { unusedTop = rope.m_UnusedLengthAtTop; } catch { /* ignore */ }
            try { unusedBottom = rope.m_UnusedLengthAtBottom; } catch { /* ignore */ }

            float usable = len - unusedTop - unusedBottom;
            if (usable > 0.01f)
            {
                lengthSource = (unusedTop > 0.01f || unusedBottom > 0.01f) ? "splineUsable" : "spline";
                return usable;
            }

            if (len > 0.01f)
            {
                lengthSource = "spline";
                return len;
            }
        }
        catch (Exception ex)
        {
            MelonLogger.Warning($"Rope spline length failed: {ex.Message}");
        }

        return null;
    }

    private static float? ParseMetersFromName(string? text)
    {
        if (string.IsNullOrWhiteSpace(text))
            return null;
        Match m = MetersInName.Match(text);
        if (!m.Success)
            return null;
        if (!float.TryParse(m.Groups[1].Value, NumberStyles.Integer, CultureInfo.InvariantCulture, out float meters))
            return null;
        return meters;
    }

    private static Vec3Dto Vec(Vector3 v) => new() { X = v.x, Y = v.y, Z = v.z };

    private static string? BuildPath(Transform tr)
    {
        try
        {
            var parts = new List<string>();
            Transform? cur = tr;
            int guard = 0;
            while (cur != null && guard++ < 16)
            {
                parts.Add(cur.name);
                cur = cur.parent;
            }
            parts.Reverse();
            return string.Join("/", parts);
        }
        catch
        {
            return null;
        }
    }

    private static void Msg(string text)
    {
        MelonLogger.Msg(text);
        try { uConsoleLog.Add("TerrainDumper: " + text); } catch { /* ignore */ }
    }

    private static readonly JsonSerializerOptions JsonOpts = new()
    {
        WriteIndented = true,
        DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull,
    };

    private sealed class ClimbingDoc
    {
        [JsonPropertyName("formatVersion")] public int FormatVersion { get; set; }
        [JsonPropertyName("sceneName")] public string SceneName { get; set; } = "";
        [JsonPropertyName("dumpedAtUtc")] public string DumpedAtUtc { get; set; } = "";
        [JsonPropertyName("modVersion")] public string ModVersion { get; set; } = "";
        [JsonPropertyName("method")] public string Method { get; set; } = "";
        [JsonPropertyName("ropeAnchorCount")] public int RopeAnchorCount { get; set; }
        [JsonPropertyName("ropeObjectCount")] public int RopeObjectCount { get; set; }
        [JsonPropertyName("ropeClimbCount")] public int RopeClimbCount { get; set; }
        [JsonPropertyName("cliffRootCount")] public int CliffRootCount { get; set; }
        [JsonPropertyName("scrambleCount")] public int ScrambleCount { get; set; }
        [JsonPropertyName("playerX")] public float? PlayerX { get; set; }
        [JsonPropertyName("playerY")] public float? PlayerY { get; set; }
        [JsonPropertyName("playerZ")] public float? PlayerZ { get; set; }
        [JsonPropertyName("ropeClimbs")] public List<RopeClimbEntry> RopeClimbs { get; set; } = new();
        [JsonPropertyName("scrambles")] public List<ScrambleEntry> Scrambles { get; set; } = new();
        [JsonPropertyName("notes")] public string[]? Notes { get; set; }
    }

    private sealed class RopeClimbEntry
    {
        [JsonPropertyName("kind")] public string Kind { get; set; } = "rope";
        [JsonPropertyName("gameObject")] public string? GameObject { get; set; }
        [JsonPropertyName("path")] public string? Path { get; set; }
        [JsonPropertyName("x")] public float X { get; set; }
        [JsonPropertyName("y")] public float Y { get; set; }
        [JsonPropertyName("z")] public float Z { get; set; }
        [JsonPropertyName("lengthMeters")] public float? LengthMeters { get; set; }
        [JsonPropertyName("lengthSource")] public string? LengthSource { get; set; }
        [JsonPropertyName("top")] public Vec3Dto? Top { get; set; }
        [JsonPropertyName("bottom")] public Vec3Dto? Bottom { get; set; }
    }

    private sealed class ScrambleEntry
    {
        [JsonPropertyName("kind")] public string Kind { get; set; } = "scramble";
        [JsonPropertyName("gameObject")] public string? GameObject { get; set; }
        [JsonPropertyName("path")] public string? Path { get; set; }
        [JsonPropertyName("x")] public float X { get; set; }
        [JsonPropertyName("y")] public float Y { get; set; }
        [JsonPropertyName("z")] public float Z { get; set; }
        [JsonPropertyName("lengthMeters")] public float? LengthMeters { get; set; }
        [JsonPropertyName("lengthSource")] public string? LengthSource { get; set; }
        [JsonPropertyName("climbUp")] public Vec3Dto? ClimbUp { get; set; }
        [JsonPropertyName("climbDown")] public Vec3Dto? ClimbDown { get; set; }
    }

    private sealed class Vec3Dto
    {
        [JsonPropertyName("x")] public float X { get; set; }
        [JsonPropertyName("y")] public float Y { get; set; }
        [JsonPropertyName("z")] public float Z { get; set; }
    }
}
