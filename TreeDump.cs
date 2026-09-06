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
/// Dump TerrainData tree instance positions for offline map stamping (path C).
/// </summary>
internal static class TreeDump
{
    public static void RunFromConsole()
    {
        try
        {
            string path = Run();
            Msg($"Tree dump wrote {path}");
        }
        catch (Exception ex)
        {
            MelonLogger.Error($"dump_trees failed: {ex}");
            try { Il2Cpp.uConsoleLog.Add($"TerrainDumper ERROR: {ex.Message}"); } catch { /* ignore */ }
        }
    }

    public static string Run()
    {
        string sceneName = UnitySceneManager.GetActiveScene().name;
        if (string.IsNullOrWhiteSpace(sceneName))
            sceneName = "UnknownScene";

        string outDir = Path.Combine(MelonEnvironment.ModsDirectory, "TerrainDumper", sceneName);
        Directory.CreateDirectory(outDir);

        Terrain[] terrains = Terrain.activeTerrains;
        if (terrains == null || terrains.Length == 0)
            throw new InvalidOperationException("No active Terrains — load a region first.");

        var prototypes = new List<ProtoMeta>();
        var instances = new List<InstMeta>();
        var protoKeyToIndex = new Dictionary<string, int>(StringComparer.Ordinal);

        int terrainIndex = 0;
        foreach (Terrain t in terrains)
        {
            if (t == null || t.terrainData == null)
            {
                terrainIndex++;
                continue;
            }

            TerrainData td = t.terrainData;
            Vector3 origin = t.transform.position;
            // Unity Terrain ignores its Transform scale; terrainData.size is the world extent.
            Vector3 size = td.size;

            TreePrototype[]? protos = td.treePrototypes;
            var localProtoMap = new int[protos?.Length ?? 0];
            if (protos != null)
            {
                for (int p = 0; p < protos.Length; p++)
                {
                    TreePrototype tp = protos[p];
                    string name = "unknown";
                    try
                    {
                        if (tp != null && tp.prefab != null)
                            name = tp.prefab.name ?? "unknown";
                    }
                    catch { name = "unknown"; }

                    string key = name;
                    if (!protoKeyToIndex.TryGetValue(key, out int globalIdx))
                    {
                        globalIdx = prototypes.Count;
                        protoKeyToIndex[key] = globalIdx;
                        prototypes.Add(new ProtoMeta
                        {
                            Index = globalIdx,
                            Name = name,
                        });
                    }
                    localProtoMap[p] = globalIdx;
                }
            }

            TreeInstance[]? trees = null;
            try { trees = td.treeInstances; }
            catch (Exception ex)
            {
                MelonLogger.Warning($"terrain_{terrainIndex:D2}: treeInstances failed: {ex.Message}");
            }

            if (trees == null)
            {
                terrainIndex++;
                continue;
            }

            for (int i = 0; i < trees.Length; i++)
            {
                TreeInstance inst = trees[i];
                int protoLocal = inst.prototypeIndex;
                int protoGlobal = (protoLocal >= 0 && protoLocal < localProtoMap.Length)
                    ? localProtoMap[protoLocal]
                    : -1;

                // TreeInstance.position is normalized 0..1 on the terrain.
                float lx = inst.position.x * size.x;
                float ly = inst.position.y * size.y;
                float lz = inst.position.z * size.z;
                float wx = origin.x + lx;
                float wy = origin.y + ly;
                float wz = origin.z + lz;

                Color32 c = inst.color;
                instances.Add(new InstMeta
                {
                    X = wx,
                    Y = wy,
                    Z = wz,
                    Prototype = protoGlobal,
                    HeightScale = inst.heightScale,
                    WidthScale = inst.widthScale,
                    R = c.r,
                    G = c.g,
                    B = c.b,
                    Terrain = terrainIndex,
                });
            }

            MelonLogger.Msg($"terrain_{terrainIndex:D2}: {trees.Length} tree instance(s).");
            terrainIndex++;
        }

        var doc = new TreeDumpDoc
        {
            FormatVersion = 1,
            SceneName = sceneName,
            DumpedAtUtc = DateTime.UtcNow.ToString("o", CultureInfo.InvariantCulture),
            ModVersion = Implementation.ModVersion,
            PrototypeCount = prototypes.Count,
            InstanceCount = instances.Count,
            Prototypes = prototypes.ToArray(),
            Instances = instances.ToArray(),
            Notes = new[]
            {
                "TerrainData treeInstances in world XYZ for offline map stamping.",
                "position was normalized on terrain; converted with terrain origin + normalized * terrainData.size.",
            },
        };

        string jsonPath = Path.Combine(outDir, "tree_instances.json");
        File.WriteAllText(
            jsonPath,
            JsonSerializer.Serialize(doc, JsonOpts),
            new UTF8Encoding(encoderShouldEmitUTF8Identifier: false));

        return jsonPath;
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

    private sealed class TreeDumpDoc
    {
        [JsonPropertyName("formatVersion")] public int FormatVersion { get; set; }
        [JsonPropertyName("sceneName")] public string SceneName { get; set; } = "";
        [JsonPropertyName("dumpedAtUtc")] public string DumpedAtUtc { get; set; } = "";
        [JsonPropertyName("modVersion")] public string ModVersion { get; set; } = "";
        [JsonPropertyName("prototypeCount")] public int PrototypeCount { get; set; }
        [JsonPropertyName("instanceCount")] public int InstanceCount { get; set; }
        [JsonPropertyName("prototypes")] public ProtoMeta[]? Prototypes { get; set; }
        [JsonPropertyName("instances")] public InstMeta[]? Instances { get; set; }
        [JsonPropertyName("notes")] public string[]? Notes { get; set; }
    }

    private sealed class ProtoMeta
    {
        [JsonPropertyName("index")] public int Index { get; set; }
        [JsonPropertyName("name")] public string Name { get; set; } = "";
    }

    private sealed class InstMeta
    {
        [JsonPropertyName("x")] public float X { get; set; }
        [JsonPropertyName("y")] public float Y { get; set; }
        [JsonPropertyName("z")] public float Z { get; set; }
        [JsonPropertyName("prototype")] public int Prototype { get; set; }
        [JsonPropertyName("heightScale")] public float HeightScale { get; set; }
        [JsonPropertyName("widthScale")] public float WidthScale { get; set; }
        [JsonPropertyName("r")] public byte R { get; set; }
        [JsonPropertyName("g")] public byte G { get; set; }
        [JsonPropertyName("b")] public byte B { get; set; }
        [JsonPropertyName("terrain")] public int Terrain { get; set; }
    }
}
