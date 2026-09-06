using System.Globalization;
using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;
using Il2Cpp;
using Il2CppInterop.Runtime;
using Il2CppInterop.Runtime.InteropTypes;
using MelonLoader;
using MelonLoader.Utils;
using UnityEngine;
using UnitySceneManager = UnityEngine.SceneManagement.SceneManager;

namespace TerrainDumper;

internal static class TerrainDump
{
    private const int FormatVersion = 2;

    private static JsonSerializerOptions? _jsonOptions;

    private static JsonSerializerOptions JsonOptions =>
        _jsonOptions ??= new JsonSerializerOptions
        {
            WriteIndented = true,
            DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull,
        };

    public static void RunFromConsole()
    {
        try
        {
            string path = Run();
            string msg = $"TerrainDumper: wrote dump to {path}";
            MelonLogger.Msg(msg);
            try { uConsoleLog.Add(msg); } catch { /* console log optional */ }
        }
        catch (Exception ex)
        {
            MelonLogger.Error($"TerrainDumper dump failed: {ex}");
            try { uConsoleLog.Add($"TerrainDumper ERROR: {ex.Message}"); } catch { /* ignore */ }
        }
    }

    public static string Run()
    {
        string sceneName = UnitySceneManager.GetActiveScene().name;
        if (string.IsNullOrWhiteSpace(sceneName))
            sceneName = "UnknownScene";

        Terrain[] terrains = CollectTerrains();
        string outDir = Path.Combine(MelonEnvironment.ModsDirectory, "TerrainDumper", sceneName);
        Directory.CreateDirectory(outDir);

        MelonLogger.Msg($"Dumping {terrains.Length} terrain tile(s) from scene '{sceneName}' → {outDir}");

        var tileStems = new List<string>();
        var previewTiles = new List<PreviewTile>();
        var tileBounds = new List<MapAlignmentDump.TerrainTileBounds>();

        for (int i = 0; i < terrains.Length; i++)
        {
            Terrain terrain = terrains[i];
            string stem = $"terrain_{i:D2}";
            tileStems.Add(stem);

            if (terrain == null || terrain.terrainData == null)
            {
                MelonLogger.Warning($"Skipping {stem}: null Terrain or TerrainData");
                WriteTileMeta(outDir, stem, new TileMeta
                {
                    Index = i,
                    Stem = stem,
                    Error = "null Terrain or TerrainData",
                });
                continue;
            }

            DumpTile(outDir, stem, i, terrain, previewTiles, tileBounds);
        }

        var sceneMeta = new SceneMeta
        {
            FormatVersion = FormatVersion,
            SceneName = sceneName,
            DumpedAtUtc = DateTime.UtcNow.ToString("o", CultureInfo.InvariantCulture),
            ModVersion = Implementation.ModVersion,
            GameVersion = Application.version,
            TerrainCount = terrains.Length,
            Tiles = tileStems,
            Notes = new[]
            {
                "Heights are Unity TerrainData.GetHeights normalized values (0–1) quantized to uint16.",
                "World height meters ≈ normalized * size.y + terrain position.y (see per-tile meta).",
                "RAW layout: row-major, Unity GetHeights order (y/x as returned); little-endian uint16.",
                "Phase 2: see fog_of_war.json and alignment_samples.json for world→map registration.",
            },
        };

        File.WriteAllText(
            Path.Combine(outDir, "meta.json"),
            JsonSerializer.Serialize(sceneMeta, JsonOptions),
            new UTF8Encoding(encoderShouldEmitUTF8Identifier: false));

        try
        {
            WritePreview(outDir, previewTiles);
        }
        catch (Exception ex)
        {
            MelonLogger.Warning($"Preview PNG skipped: {ex.Message}");
        }

        try
        {
            MapAlignmentDump.Write(outDir, sceneName, tileBounds);
        }
        catch (Exception ex)
        {
            MelonLogger.Warning($"Map alignment dump skipped: {ex}");
        }

        return outDir;
    }

    private static void DumpTile(
        string outDir,
        string stem,
        int index,
        Terrain terrain,
        List<PreviewTile> previewTiles,
        List<MapAlignmentDump.TerrainTileBounds> tileBounds)
    {
        TerrainData data = terrain.terrainData;
        int res = data.heightmapResolution;
        Vector3 size = data.size;
        Transform t = terrain.transform;
        Vector3 pos = t.position;
        Vector3 euler = t.rotation.eulerAngles;
        Vector3 lossy = t.lossyScale;

        // Unity terrain origin is corner; AABB in world XZ from position + size (assuming no
        // rotation). Terrain ignores its Transform scale, so lossyScale is recorded for reference
        // only — TLD's water terrains carry bogus scales (Terrain_CoastalWater is 326x).
        Vector3 min = pos;
        Vector3 max = pos + size;

        tileBounds.Add(new MapAlignmentDump.TerrainTileBounds
        {
            Stem = stem,
            MinX = min.x,
            MaxX = max.x,
            MinZ = min.z,
            MaxZ = max.z,
            PosY = pos.y,
            SizeX = size.x,
            SizeZ = size.z,
            HeightmapResolution = res,
        });

        float[,] heights = ReadHeights(data, res);
        string rawPath = Path.Combine(outDir, $"{stem}_heights.raw");
        WriteHeightsRaw(rawPath, heights, res, res);

        float minH = 1f, maxH = 0f;
        for (int y = 0; y < res; y++)
        {
            for (int x = 0; x < res; x++)
            {
                float h = heights[y, x];
                if (h < minH) minH = h;
                if (h > maxH) maxH = h;
            }
        }

        var meta = new TileMeta
        {
            Index = index,
            Stem = stem,
            GameObjectName = terrain.gameObject != null ? terrain.gameObject.name : null,
            HeightmapResolution = res,
            SampleWidth = res,
            SampleHeight = res,
            Size = new Vec3Dto(size.x, size.y, size.z),
            Position = new Vec3Dto(pos.x, pos.y, pos.z),
            RotationEuler = new Vec3Dto(euler.x, euler.y, euler.z),
            LossyScale = new Vec3Dto(lossy.x, lossy.y, lossy.z),
            BoundsMin = new Vec3Dto(min.x, min.y, min.z),
            BoundsMax = new Vec3Dto(max.x, max.y, max.z),
            HeightNormalizedMin = minH,
            HeightNormalizedMax = maxH,
            Raw = new RawLayout
            {
                FileName = $"{stem}_heights.raw",
                Width = res,
                Height = res,
                Dtype = "uint16",
                Endianness = "little",
                RowOrder = "GetHeights[y, x]; y is first dimension (Unity)",
                ValueMapping = "uint16 = round(normalized * 65535); meters ≈ normalized * size.y + position.y",
            },
        };

        WriteTileMeta(outDir, stem, meta);

        previewTiles.Add(new PreviewTile
        {
            Heights = heights,
            Resolution = res,
            PosX = pos.x,
            PosZ = pos.z,
            SizeX = size.x * lossy.x,
            SizeZ = size.z * lossy.z,
        });

        MelonLogger.Msg($"  {stem}: {res}x{res}, size=({size.x},{size.y},{size.z}), pos=({pos.x:F1},{pos.y:F1},{pos.z:F1})");
    }

    private static Terrain[] CollectTerrains()
    {
        Il2CppInterop.Runtime.InteropTypes.Arrays.Il2CppReferenceArray<Terrain>? active = Terrain.activeTerrains;
        if (active == null || active.Length == 0)
            return Array.Empty<Terrain>();

        var list = new List<Terrain>(active.Length);
        for (int i = 0; i < active.Length; i++)
        {
            Terrain? t = active[i];
            if (t != null)
                list.Add(t);
        }

        return list.ToArray();
    }

    /// <summary>
    /// Il2Cpp GetHeights returns Il2CppObjectBase (float[,]). Copy into a managed array.
    /// Falls back to per-sample GetHeight (meters → normalized) if cast fails.
    /// </summary>
    private static float[,] ReadHeights(TerrainData data, int res)
    {
        try
        {
            Il2CppObjectBase raw = data.GetHeights(0, 0, res, res);
            var arr = raw.Cast<Il2CppSystem.Array>();
            var heights = new float[res, res];
            for (int y = 0; y < res; y++)
            {
                for (int x = 0; x < res; x++)
                {
                    Il2CppSystem.Object boxed = arr.GetValue(y, x);
                    heights[y, x] = boxed.Unbox<float>();
                }
            }

            return heights;
        }
        catch (Exception ex)
        {
            MelonLogger.Warning($"GetHeights cast failed ({ex.Message}); falling back to GetHeight samples");
            float maxY = Mathf.Max(0.0001f, data.size.y);
            var heights = new float[res, res];
            for (int y = 0; y < res; y++)
            {
                for (int x = 0; x < res; x++)
                    heights[y, x] = Mathf.Clamp01(data.GetHeight(x, y) / maxY);
            }

            return heights;
        }
    }

    private static void WriteTileMeta(string outDir, string stem, TileMeta meta)
    {
        File.WriteAllText(
            Path.Combine(outDir, $"{stem}_meta.json"),
            JsonSerializer.Serialize(meta, JsonOptions),
            Encoding.UTF8);
    }

    private static void WriteHeightsRaw(string path, float[,] heights, int width, int height)
    {
        // height = y dim, width = x dim matching GetHeights
        byte[] buffer = new byte[checked(width * height * 2)];
        int o = 0;
        for (int y = 0; y < height; y++)
        {
            for (int x = 0; x < width; x++)
            {
                float n = Mathf.Clamp01(heights[y, x]);
                ushort q = (ushort)Mathf.RoundToInt(n * 65535f);
                buffer[o++] = (byte)(q & 0xFF);
                buffer[o++] = (byte)(q >> 8);
            }
        }

        File.WriteAllBytes(path, buffer);
    }

    private static void WritePreview(string outDir, List<PreviewTile> tiles)
    {
        if (tiles.Count == 0)
            return;

        // Per-tile previews
        foreach (var tile in tiles)
        {
            int res = tile.Resolution;
            var tex = new Texture2D(res, res, TextureFormat.RGB24, false);
            for (int y = 0; y < res; y++)
            {
                for (int x = 0; x < res; x++)
                {
                    float n = Mathf.Clamp01(tile.Heights[y, x]);
                    tex.SetPixel(x, y, new Color(n, n, n, 1f));
                }
            }

            tex.Apply(false, false);
            byte[] png = ImageConversion.EncodeToPNG(tex);
            // stem inferred from order — write numbered
            int idx = tiles.IndexOf(tile);
            File.WriteAllBytes(Path.Combine(outDir, $"terrain_{idx:D2}_preview.png"), png);
            UnityEngine.Object.Destroy(tex);
        }

        // Combined world-XZ mosaic (downsampled)
        float minX = tiles.Min(t => t.PosX);
        float minZ = tiles.Min(t => t.PosZ);
        float maxX = tiles.Max(t => t.PosX + t.SizeX);
        float maxZ = tiles.Max(t => t.PosZ + t.SizeZ);
        float worldW = Math.Max(1f, maxX - minX);
        float worldH = Math.Max(1f, maxZ - minZ);

        const int mosaicSize = 1024;
        var mosaic = new Texture2D(mosaicSize, mosaicSize, TextureFormat.RGB24, false);
        // fill dark
        var fill = new Color[mosaicSize * mosaicSize];
        for (int i = 0; i < fill.Length; i++)
            fill[i] = new Color(0.05f, 0.05f, 0.08f, 1f);
        mosaic.SetPixels(fill);

        foreach (var tile in tiles)
        {
            int res = tile.Resolution;
            for (int y = 0; y < res; y++)
            {
                for (int x = 0; x < res; x++)
                {
                    float wx = tile.PosX + (x / (float)(res - 1)) * tile.SizeX;
                    float wz = tile.PosZ + (y / (float)(res - 1)) * tile.SizeZ;
                    int px = Mathf.Clamp(Mathf.RoundToInt((wx - minX) / worldW * (mosaicSize - 1)), 0, mosaicSize - 1);
                    int pz = Mathf.Clamp(Mathf.RoundToInt((wz - minZ) / worldH * (mosaicSize - 1)), 0, mosaicSize - 1);
                    float n = Mathf.Clamp01(tile.Heights[y, x]);
                    mosaic.SetPixel(px, pz, new Color(n, n, n, 1f));
                }
            }
        }

        mosaic.Apply(false, false);
        File.WriteAllBytes(Path.Combine(outDir, "preview_height.png"), ImageConversion.EncodeToPNG(mosaic));
        UnityEngine.Object.Destroy(mosaic);
    }

    private sealed class PreviewTile
    {
        public float[,] Heights = null!;
        public int Resolution;
        public float PosX;
        public float PosZ;
        public float SizeX;
        public float SizeZ;
    }

    private sealed class SceneMeta
    {
        [JsonPropertyName("formatVersion")]
        public int FormatVersion { get; set; }

        [JsonPropertyName("sceneName")]
        public string SceneName { get; set; } = "";

        [JsonPropertyName("dumpedAtUtc")]
        public string DumpedAtUtc { get; set; } = "";

        [JsonPropertyName("modVersion")]
        public string ModVersion { get; set; } = "";

        [JsonPropertyName("gameVersion")]
        public string? GameVersion { get; set; }

        [JsonPropertyName("terrainCount")]
        public int TerrainCount { get; set; }

        [JsonPropertyName("tiles")]
        public List<string> Tiles { get; set; } = new();

        [JsonPropertyName("notes")]
        public string[]? Notes { get; set; }
    }

    private sealed class TileMeta
    {
        [JsonPropertyName("index")]
        public int Index { get; set; }

        [JsonPropertyName("stem")]
        public string Stem { get; set; } = "";

        [JsonPropertyName("gameObjectName")]
        public string? GameObjectName { get; set; }

        [JsonPropertyName("error")]
        public string? Error { get; set; }

        [JsonPropertyName("heightmapResolution")]
        public int? HeightmapResolution { get; set; }

        [JsonPropertyName("sampleWidth")]
        public int? SampleWidth { get; set; }

        [JsonPropertyName("sampleHeight")]
        public int? SampleHeight { get; set; }

        [JsonPropertyName("size")]
        public Vec3Dto? Size { get; set; }

        [JsonPropertyName("position")]
        public Vec3Dto? Position { get; set; }

        [JsonPropertyName("rotationEuler")]
        public Vec3Dto? RotationEuler { get; set; }

        [JsonPropertyName("lossyScale")]
        public Vec3Dto? LossyScale { get; set; }

        [JsonPropertyName("boundsMin")]
        public Vec3Dto? BoundsMin { get; set; }

        [JsonPropertyName("boundsMax")]
        public Vec3Dto? BoundsMax { get; set; }

        [JsonPropertyName("heightNormalizedMin")]
        public float? HeightNormalizedMin { get; set; }

        [JsonPropertyName("heightNormalizedMax")]
        public float? HeightNormalizedMax { get; set; }

        [JsonPropertyName("raw")]
        public RawLayout? Raw { get; set; }
    }

    private sealed class RawLayout
    {
        [JsonPropertyName("fileName")]
        public string FileName { get; set; } = "";

        [JsonPropertyName("width")]
        public int Width { get; set; }

        [JsonPropertyName("height")]
        public int Height { get; set; }

        [JsonPropertyName("dtype")]
        public string Dtype { get; set; } = "";

        [JsonPropertyName("endianness")]
        public string Endianness { get; set; } = "";

        [JsonPropertyName("rowOrder")]
        public string RowOrder { get; set; } = "";

        [JsonPropertyName("valueMapping")]
        public string ValueMapping { get; set; } = "";
    }

    private sealed class Vec3Dto
    {
        public Vec3Dto(float x, float y, float z)
        {
            X = x;
            Y = y;
            Z = z;
        }

        [JsonPropertyName("x")]
        public float X { get; set; }

        [JsonPropertyName("y")]
        public float Y { get; set; }

        [JsonPropertyName("z")]
        public float Z { get; set; }
    }
}
