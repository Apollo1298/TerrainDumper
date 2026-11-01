using System.Globalization;
using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;
using Il2Cpp;
using MelonLoader;
using MelonLoader.Utils;
using UnityEngine;
using UnitySceneManager = UnityEngine.SceneManagement.SceneManager;

namespace TerrainDumper;

/// <summary>
/// Dump fallen / bridge / tree-log prop roots (name-matched) for map stamping.
/// Broad first pass so kinds can be narrowed offline; not part of dump_map.
/// </summary>
internal static class FallenLogsDump
{
    private const int FormatVersion = 1;

    public static void RunFromConsole() => Run();

    public static void Run()
    {
        try
        {
            Dump();
        }
        catch (Exception ex)
        {
            MelonLogger.Error($"dump_fallen_logs failed: {ex}");
            try { uConsoleLog.Add($"TerrainDumper ERROR: {ex.Message}"); } catch { /* ignore */ }
        }
    }

    private static void Dump()
    {
        string sceneName = UnitySceneManager.GetActiveScene().name;
        if (string.IsNullOrWhiteSpace(sceneName))
            sceneName = "UnknownScene";

        string outDir = Path.Combine(MelonEnvironment.ModsDirectory, "TerrainDumper", sceneName);
        Directory.CreateDirectory(outDir);

        Transform[]? transforms = null;
        try { transforms = UnityEngine.Object.FindObjectsOfType<Transform>(true); }
        catch (Exception ex)
        {
            throw new InvalidOperationException($"FindObjectsOfType<Transform> failed: {ex.Message}", ex);
        }

        int scanned = transforms?.Length ?? 0;
        int nameHits = 0;
        int skippedChild = 0;
        var logs = new List<FallenLogEntry>();

        if (transforms != null)
        {
            foreach (Transform tr in transforms)
            {
                if (tr == null)
                    continue;

                string n;
                try { n = tr.name ?? ""; }
                catch { continue; }

                if (!TryClassify(n, out string kind))
                    continue;

                nameHits++;

                // Keep outermost matching root only (skip LOD / mesh children under a matching parent).
                Transform? parent = null;
                try { parent = tr.parent; } catch { /* ignore */ }
                string parentName = "";
                try { parentName = parent != null ? (parent.name ?? "") : ""; } catch { /* ignore */ }
                if (TryClassify(parentName, out _))
                {
                    skippedChild++;
                    continue;
                }

                Vector3 pos;
                string? path;
                try
                {
                    pos = tr.position;
                    path = BuildPath(tr);
                }
                catch
                {
                    continue;
                }

                Bounds? worldBounds = TryWorldBounds(tr);

                logs.Add(new FallenLogEntry
                {
                    Kind = kind,
                    GameObject = string.IsNullOrWhiteSpace(n) ? null : n,
                    Path = path,
                    X = pos.x,
                    Y = pos.y,
                    Z = pos.z,
                    BoundsMin = worldBounds == null ? null : Vec(worldBounds.Value.min),
                    BoundsMax = worldBounds == null ? null : Vec(worldBounds.Value.max),
                    BoundsSize = worldBounds == null ? null : Vec(worldBounds.Value.size),
                });
            }
        }

        logs.Sort((a, b) =>
        {
            int c = string.CompareOrdinal(a.Kind ?? "", b.Kind ?? "");
            if (c != 0) return c;
            c = string.CompareOrdinal(a.GameObject ?? "", b.GameObject ?? "");
            if (c != 0) return c;
            return a.X.CompareTo(b.X);
        });

        var kindCounts = new Dictionary<string, int>(StringComparer.Ordinal);
        foreach (FallenLogEntry e in logs)
        {
            string k = e.Kind ?? "unknown";
            kindCounts[k] = kindCounts.TryGetValue(k, out int c) ? c + 1 : 1;
        }

        Vector3? playerPos = null;
        try
        {
            Transform? pt = GameManager.GetPlayerTransform();
            if (pt != null)
                playerPos = pt.position;
        }
        catch { /* ignore */ }

        var doc = new FallenLogsDoc
        {
            FormatVersion = FormatVersion,
            SceneName = sceneName,
            DumpedAtUtc = DateTime.UtcNow.ToString("o", CultureInfo.InvariantCulture),
            ModVersion = Implementation.ModVersion,
            Method = "FindObjectsOfType<Transform> name-match (outermost roots)",
            ScannedTransformCount = scanned,
            NameHitCount = nameHits,
            SkippedChildCount = skippedChild,
            LogCount = logs.Count,
            KindCounts = kindCounts,
            PlayerX = playerPos?.x,
            PlayerY = playerPos?.y,
            PlayerZ = playerPos?.z,
            Logs = logs,
            Notes = new[]
            {
                "Broad candidate pass for fallen / bridge / tree-log props. Refine kinds offline.",
                "kind: bridge | fallen | pineLog | treeLog | railLog | log",
                "Outer matching roots only (LOD/mesh children under a matching parent are skipped).",
                "bounds* from Renderer or Collider world AABB when available.",
                "Cross-check with console `pos` vs playerX/Y/Z to identify walkable types near you.",
            },
        };

        string pathOut = Path.Combine(outDir, "fallen_logs.json");
        File.WriteAllText(
            pathOut,
            JsonSerializer.Serialize(doc, JsonOpts),
            new UTF8Encoding(encoderShouldEmitUTF8Identifier: false));

        string kinds = string.Join(", ", kindCounts.OrderBy(kv => kv.Key).Select(kv => $"{kv.Key}={kv.Value}"));
        Msg($"Fallen logs done → {pathOut} (logs={logs.Count}, scanned={scanned}; {kinds})");
    }

    /// <summary>True if <paramref name="name"/> looks like a log/fallen-timber prop; sets <paramref name="kind"/>.</summary>
    private static bool TryClassify(string name, out string kind)
    {
        kind = "";
        if (string.IsNullOrWhiteSpace(name))
            return false;

        // Rail-car timber stacks (usually not walkable bridges).
        if (Contains(name, "LogRailCar") || Contains(name, "RailCar") && Contains(name, "Log"))
        {
            kind = "railLog";
            return true;
        }

        if (Contains(name, "LogBridge") || Contains(name, "Log_Bridge"))
        {
            kind = "bridge";
            return true;
        }

        if (Contains(name, "LogFallen") || Contains(name, "FallenLog") || Contains(name, "Fallen_Log"))
        {
            kind = "fallen";
            return true;
        }

        if (Contains(name, "PineTreeLog"))
        {
            kind = "pineLog";
            return true;
        }

        if (Contains(name, "TreeLog") || Contains(name, "Tree_Log"))
        {
            kind = "treeLog";
            return true;
        }

        // Generic OBJ_Log* / TRN_Log* leftover (piles, singles, etc.).
        if (StartsWith(name, "OBJ_Log") || StartsWith(name, "TRN_Log"))
        {
            kind = "log";
            return true;
        }

        return false;
    }

    private static bool Contains(string hay, string needle) =>
        hay.IndexOf(needle, StringComparison.OrdinalIgnoreCase) >= 0;

    private static bool StartsWith(string hay, string needle) =>
        hay.StartsWith(needle, StringComparison.OrdinalIgnoreCase);

    private static Bounds? TryWorldBounds(Transform tr)
    {
        try
        {
            Renderer[] renderers = tr.GetComponentsInChildren<Renderer>(true);
            Bounds? b = null;
            if (renderers != null)
            {
                foreach (Renderer r in renderers)
                {
                    if (r == null)
                        continue;
                    try
                    {
                        Bounds rb = r.bounds;
                        if (b == null) b = rb;
                        else
                        {
                            Bounds cur = b.Value;
                            cur.Encapsulate(rb);
                            b = cur;
                        }
                    }
                    catch { /* ignore */ }
                }
            }

            if (b != null)
                return b;
        }
        catch { /* ignore */ }

        try
        {
            Collider[] cols = tr.GetComponentsInChildren<Collider>(true);
            Bounds? b = null;
            if (cols != null)
            {
                foreach (Collider c in cols)
                {
                    if (c == null)
                        continue;
                    try
                    {
                        Bounds cb = c.bounds;
                        if (b == null) b = cb;
                        else
                        {
                            Bounds cur = b.Value;
                            cur.Encapsulate(cb);
                            b = cur;
                        }
                    }
                    catch { /* ignore */ }
                }
            }

            return b;
        }
        catch
        {
            return null;
        }
    }

    private static string? BuildPath(Transform tr)
    {
        try
        {
            var parts = new List<string>(8);
            Transform? cur = tr;
            for (int i = 0; i < 24 && cur != null; i++)
            {
                parts.Add(cur.name ?? "?");
                try { cur = cur.parent; }
                catch { break; }
            }

            parts.Reverse();
            return string.Join("/", parts);
        }
        catch
        {
            return null;
        }
    }

    private static Vec3Dto Vec(Vector3 v) => new() { X = v.x, Y = v.y, Z = v.z };

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

    private sealed class FallenLogsDoc
    {
        [JsonPropertyName("formatVersion")] public int FormatVersion { get; set; }
        [JsonPropertyName("sceneName")] public string SceneName { get; set; } = "";
        [JsonPropertyName("dumpedAtUtc")] public string DumpedAtUtc { get; set; } = "";
        [JsonPropertyName("modVersion")] public string ModVersion { get; set; } = "";
        [JsonPropertyName("method")] public string Method { get; set; } = "";
        [JsonPropertyName("scannedTransformCount")] public int ScannedTransformCount { get; set; }
        [JsonPropertyName("nameHitCount")] public int NameHitCount { get; set; }
        [JsonPropertyName("skippedChildCount")] public int SkippedChildCount { get; set; }
        [JsonPropertyName("logCount")] public int LogCount { get; set; }
        [JsonPropertyName("kindCounts")] public Dictionary<string, int>? KindCounts { get; set; }
        [JsonPropertyName("playerX")] public float? PlayerX { get; set; }
        [JsonPropertyName("playerY")] public float? PlayerY { get; set; }
        [JsonPropertyName("playerZ")] public float? PlayerZ { get; set; }
        [JsonPropertyName("logs")] public List<FallenLogEntry> Logs { get; set; } = new();
        [JsonPropertyName("notes")] public string[]? Notes { get; set; }
    }

    private sealed class FallenLogEntry
    {
        [JsonPropertyName("kind")] public string Kind { get; set; } = "";
        [JsonPropertyName("gameObject")] public string? GameObject { get; set; }
        [JsonPropertyName("path")] public string? Path { get; set; }
        [JsonPropertyName("x")] public float X { get; set; }
        [JsonPropertyName("y")] public float Y { get; set; }
        [JsonPropertyName("z")] public float Z { get; set; }
        [JsonPropertyName("boundsMin")] public Vec3Dto? BoundsMin { get; set; }
        [JsonPropertyName("boundsMax")] public Vec3Dto? BoundsMax { get; set; }
        [JsonPropertyName("boundsSize")] public Vec3Dto? BoundsSize { get; set; }
    }

    private sealed class Vec3Dto
    {
        [JsonPropertyName("x")] public float X { get; set; }
        [JsonPropertyName("y")] public float Y { get; set; }
        [JsonPropertyName("z")] public float Z { get; set; }
    }
}
