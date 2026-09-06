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
/// Dump LoadScene transition contacts in the active scene (portal graph seeds).
/// </summary>
internal static class PortalDump
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
            MelonLogger.Error($"dump_portals failed: {ex}");
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

        LoadScene[]? loads = null;
        try
        {
            loads = UnityEngine.Object.FindObjectsOfType<LoadScene>(true);
        }
        catch (Exception ex)
        {
            throw new InvalidOperationException($"FindObjectsOfType<LoadScene> failed: {ex.Message}", ex);
        }

        var portals = new List<PortalEntry>();
        int skippedEmpty = 0;
        if (loads != null)
        {
            foreach (LoadScene ls in loads)
            {
                if (ls == null)
                    continue;

                string dest;
                try { dest = ls.GetSceneToLoad() ?? ls.m_SceneToLoad ?? ""; }
                catch { dest = ls.m_SceneToLoad ?? ""; }

                if (string.IsNullOrWhiteSpace(dest))
                {
                    skippedEmpty++;
                    continue;
                }

                string exitName;
                try { exitName = ls.m_ExitPointName ?? ""; }
                catch { exitName = ""; }

                string locId;
                try { locId = ls.m_SceneLocationLocIDToShow ?? ""; }
                catch { locId = ""; }

                string guid;
                try { guid = ls.GetGUID() ?? ls.m_GUID ?? ""; }
                catch { guid = ls.m_GUID ?? ""; }

                bool onContact = false;
                try { onContact = ls.m_TransitionOnContact; }
                catch { /* ignore */ }

                Vector3 pos;
                string goName = "";
                try
                {
                    Transform tr = ls.transform;
                    pos = tr.position;
                    goName = ls.gameObject != null ? ls.gameObject.name : "";
                }
                catch
                {
                    continue;
                }

                portals.Add(new PortalEntry
                {
                    FromScene = sceneName,
                    ToScene = dest.Trim(),
                    ExitPointName = string.IsNullOrWhiteSpace(exitName) ? null : exitName.Trim(),
                    SceneLocationLocId = string.IsNullOrWhiteSpace(locId) ? null : locId.Trim(),
                    Guid = string.IsNullOrWhiteSpace(guid) ? null : guid.Trim(),
                    TransitionOnContact = onContact,
                    GameObject = string.IsNullOrWhiteSpace(goName) ? null : goName,
                    X = pos.x,
                    Y = pos.y,
                    Z = pos.z,
                });
            }
        }

        // Stable order for diffs.
        portals.Sort((a, b) =>
        {
            int c = string.CompareOrdinal(a.ToScene, b.ToScene);
            if (c != 0) return c;
            c = string.CompareOrdinal(a.ExitPointName ?? "", b.ExitPointName ?? "");
            if (c != 0) return c;
            return a.X.CompareTo(b.X);
        });

        // Named exit-point transforms in this scene (incoming spawn markers).
        var exitPoints = new List<ExitPointEntry>();
        try
        {
            Transform[]? transforms = UnityEngine.Object.FindObjectsOfType<Transform>(true);
            if (transforms != null)
            {
                var seen = new HashSet<string>(StringComparer.Ordinal);
                foreach (Transform tr in transforms)
                {
                    if (tr == null)
                        continue;
                    string n = tr.name ?? "";
                    if (n.IndexOf("ExitPoint", StringComparison.OrdinalIgnoreCase) < 0
                        && n.IndexOf("EnterPoint", StringComparison.OrdinalIgnoreCase) < 0)
                        continue;
                    // Prefer leaf-ish names; skip huge hierarchies by requiring short path depth.
                    string key = $"{n}|{tr.position.x:F2}|{tr.position.z:F2}";
                    if (!seen.Add(key))
                        continue;
                    Vector3 p = tr.position;
                    exitPoints.Add(new ExitPointEntry
                    {
                        Name = n,
                        X = p.x,
                        Y = p.y,
                        Z = p.z,
                        Path = BuildPath(tr),
                    });
                }
            }
        }
        catch (Exception ex)
        {
            MelonLogger.Warning($"ExitPoint scan failed: {ex.Message}");
        }

        exitPoints.Sort((a, b) => string.CompareOrdinal(a.Name, b.Name));

        Vector3? playerPos = null;
        try
        {
            Transform? pt = GameManager.GetPlayerTransform();
            if (pt != null)
                playerPos = pt.position;
        }
        catch { /* ignore */ }

        var doc = new PortalDoc
        {
            FormatVersion = FormatVersion,
            SceneName = sceneName,
            DumpedAtUtc = DateTime.UtcNow.ToString("o", CultureInfo.InvariantCulture),
            ModVersion = Implementation.ModVersion,
            Method = "FindObjectsOfType<LoadScene> + ExitPoint/EnterPoint transforms",
            LoadSceneCount = loads?.Length ?? 0,
            PortalCount = portals.Count,
            SkippedEmptySceneToLoad = skippedEmpty,
            ExitPointMarkerCount = exitPoints.Count,
            PlayerX = playerPos?.x,
            PlayerY = playerPos?.y,
            PlayerZ = playerPos?.z,
            Portals = portals,
            ExitPointMarkers = exitPoints,
            Notes = new[]
            {
                "portals[]: LoadScene triggers in this scene; toScene + exitPointName is the spawn in the destination.",
                "x/y/z: world position of the LoadScene GameObject (contact / door).",
                "exitPointMarkers[]: transforms in THIS scene whose names contain ExitPoint or EnterPoint (incoming spawns).",
                "Match offline: portal(from A).exitPointName ↔ exitPointMarkers(name) in destination scene B.",
            },
        };

        string path = Path.Combine(outDir, "portals.json");
        File.WriteAllText(
            path,
            JsonSerializer.Serialize(doc, JsonOpts),
            new UTF8Encoding(encoderShouldEmitUTF8Identifier: false));

        Msg($"Portals done → {path} (loadScenes={doc.LoadSceneCount}, portals={portals.Count}, markers={exitPoints.Count}, skippedEmpty={skippedEmpty})");
    }

    private static string? BuildPath(Transform tr)
    {
        try
        {
            var parts = new List<string>();
            Transform? cur = tr;
            int guard = 0;
            while (cur != null && guard++ < 12)
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

    private sealed class PortalDoc
    {
        [JsonPropertyName("formatVersion")] public int FormatVersion { get; set; }
        [JsonPropertyName("sceneName")] public string SceneName { get; set; } = "";
        [JsonPropertyName("dumpedAtUtc")] public string DumpedAtUtc { get; set; } = "";
        [JsonPropertyName("modVersion")] public string ModVersion { get; set; } = "";
        [JsonPropertyName("method")] public string Method { get; set; } = "";
        [JsonPropertyName("loadSceneCount")] public int LoadSceneCount { get; set; }
        [JsonPropertyName("portalCount")] public int PortalCount { get; set; }
        [JsonPropertyName("skippedEmptySceneToLoad")] public int SkippedEmptySceneToLoad { get; set; }
        [JsonPropertyName("exitPointMarkerCount")] public int ExitPointMarkerCount { get; set; }
        [JsonPropertyName("playerX")] public float? PlayerX { get; set; }
        [JsonPropertyName("playerY")] public float? PlayerY { get; set; }
        [JsonPropertyName("playerZ")] public float? PlayerZ { get; set; }
        [JsonPropertyName("portals")] public List<PortalEntry> Portals { get; set; } = new();
        [JsonPropertyName("exitPointMarkers")] public List<ExitPointEntry> ExitPointMarkers { get; set; } = new();
        [JsonPropertyName("notes")] public string[]? Notes { get; set; }
    }

    private sealed class PortalEntry
    {
        [JsonPropertyName("fromScene")] public string FromScene { get; set; } = "";
        [JsonPropertyName("toScene")] public string ToScene { get; set; } = "";
        [JsonPropertyName("exitPointName")] public string? ExitPointName { get; set; }
        [JsonPropertyName("sceneLocationLocId")] public string? SceneLocationLocId { get; set; }
        [JsonPropertyName("guid")] public string? Guid { get; set; }
        [JsonPropertyName("transitionOnContact")] public bool TransitionOnContact { get; set; }
        [JsonPropertyName("gameObject")] public string? GameObject { get; set; }
        [JsonPropertyName("x")] public float X { get; set; }
        [JsonPropertyName("y")] public float Y { get; set; }
        [JsonPropertyName("z")] public float Z { get; set; }
    }

    private sealed class ExitPointEntry
    {
        [JsonPropertyName("name")] public string Name { get; set; } = "";
        [JsonPropertyName("x")] public float X { get; set; }
        [JsonPropertyName("y")] public float Y { get; set; }
        [JsonPropertyName("z")] public float Z { get; set; }
        [JsonPropertyName("path")] public string? Path { get; set; }
    }
}
