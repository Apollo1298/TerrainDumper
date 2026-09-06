using System.Globalization;
using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;
using Il2Cpp;
using MelonLoader;
using UnityEngine;

namespace TerrainDumper;

internal static class MapAlignmentDump
{
    public static void Write(string outDir, string sceneName, IReadOnlyList<TerrainTileBounds> tiles)
    {
        var fogPayload = new FogOfWarDumpFile
        {
            FormatVersion = 2,
            SceneName = sceneName,
            DumpedAtUtc = DateTime.UtcNow.ToString("o", CultureInfo.InvariantCulture),
            Notes = new[]
            {
                "FogOfWar heightmap UV typically uses m_HeightmapScale / m_HeightmapOffset with world XZ.",
                "Panel_Map.WorldPositionToMapPosition maps world → map UI space; see alignment_samples.json.",
                "Open the region map once before dumping if fog fields/textures are null.",
            },
        };

        Panel_Map? panel = TryGetPanelMap();
        if (panel != null)
        {
            fogPayload.PanelMapFound = true;
            try { fogPayload.MapNameOfCurrentScene = panel.GetMapNameOfCurrentScene(); } catch { /* optional */ }
            try { fogPayload.MapNameOfScene = panel.GetMapNameOfScene(sceneName); } catch { /* optional */ }
            fogPayload.MapRadiusConstant = Panel_Map.MAP_RADIUS;
            fogPayload.FogOfWarRadiusMultiplier = Panel_Map.FOGOFWAR_RADIUS_MULTIPLIER;
        }
        else
        {
            fogPayload.PanelMapFound = false;
            MelonLogger.Warning("Panel_Map not available — alignment samples skipped. Open the map UI once, then re-dump.");
        }

        FogOfWar? fog = null;
        if (panel != null)
        {
            try
            {
                var dict = panel.m_FogOfWar;
                if (dict != null)
                {
                    if (!string.IsNullOrEmpty(fogPayload.MapNameOfCurrentScene) &&
                        dict.ContainsKey(fogPayload.MapNameOfCurrentScene))
                    {
                        fog = dict[fogPayload.MapNameOfCurrentScene];
                    }
                    else if (dict.ContainsKey(sceneName))
                    {
                        fog = dict[sceneName];
                    }
                    else
                    {
                        foreach (var kv in dict)
                        {
                            fog = kv.Value;
                            fogPayload.FogOfWarDictionaryKey = kv.Key;
                            break;
                        }
                    }

                    fogPayload.FogOfWarDictionaryKeys = new List<string>();
                    foreach (var kv in dict)
                        fogPayload.FogOfWarDictionaryKeys.Add(kv.Key);
                }
            }
            catch (Exception ex)
            {
                MelonLogger.Warning($"Reading Panel_Map.m_FogOfWar failed: {ex.Message}");
            }
        }

        if (fog == null)
            fog = FindFogOfWar();

        if (fog != null)
        {
            fogPayload.FogOfWarFound = true;
            CaptureFog(fog, fogPayload);
        }
        else
        {
            fogPayload.FogOfWarFound = false;
            MelonLogger.Warning("FogOfWar instance not found. Open charcoal map in this region, then re-dump.");
        }

        File.WriteAllText(
            Path.Combine(outDir, "fog_of_war.json"),
            JsonSerializer.Serialize(fogPayload, JsonOpts),
            new UTF8Encoding(encoderShouldEmitUTF8Identifier: false));

        if (panel != null && tiles.Count > 0)
            WriteSamples(outDir, sceneName, panel, tiles);
    }

    private static void CaptureFog(FogOfWar fog, FogOfWarDumpFile dst)
    {
        dst.MapBackgroundFilename = fog.m_MapBackgroundFilename;
        dst.MapDetailsFilename = fog.m_MapDetailsFilename;
        dst.HeightmapFilename = fog.m_HeightmapFilename;
        dst.DetailScale = V2(fog.m_DetailScale);
        dst.HeightmapOffset = V2(fog.m_HeightmapOffset);
        dst.HeightmapScale = V2(fog.m_HeightmapScale);
        dst.TerrainPositionY = fog.m_TerrainPositionY;
        dst.TerrainMaxHeight = fog.m_TerrainMaxHeight;
        dst.VisibleHeightScalar = fog.m_VisibleHeightScalar;
        dst.FadeDistance = fog.m_FadeDistance;
        dst.SceneRadius = fog.m_SceneRadius;
        dst.MapRadius = fog.m_MapRadius;

        Texture2D? bg = fog.m_MapBackground;
        if (bg != null)
        {
            dst.MapBackgroundWidth = bg.width;
            dst.MapBackgroundHeight = bg.height;
        }

        Texture2D? details = fog.m_MapDetails;
        if (details != null)
        {
            dst.MapDetailsWidth = details.width;
            dst.MapDetailsHeight = details.height;
        }

        Texture2D? height = fog.m_Heightmap;
        if (height != null)
        {
            dst.HeightmapWidth = height.width;
            dst.HeightmapHeight = height.height;
        }

        MelonLogger.Msg(
            $"FogOfWar: bg='{dst.MapBackgroundFilename}' scale={fog.m_HeightmapScale} offset={fog.m_HeightmapOffset} " +
            $"sceneR={fog.m_SceneRadius} mapR={fog.m_MapRadius}");
    }

    private static void WriteSamples(
        string outDir,
        string sceneName,
        Panel_Map panel,
        IReadOnlyList<TerrainTileBounds> tiles)
    {
        var samples = new AlignmentSamplesFile
        {
            FormatVersion = 2,
            SceneName = sceneName,
            MapSpaceNotes = new[]
            {
                "worldPosition = Unity world XYZ",
                "mapPosition = Panel_Map.WorldPositionToMapPosition(scene, world) as Vector3 (x/y are map plane; z often unused)",
                "Corners are terrain AABB corners at terrain Y; grid is XZ samples on main (largest) tile.",
            },
            Samples = new List<AlignmentSample>(),
        };

        foreach (var tile in tiles)
        {
            Vector3[] corners =
            {
                new(tile.MinX, tile.PosY, tile.MinZ),
                new(tile.MaxX, tile.PosY, tile.MinZ),
                new(tile.MinX, tile.PosY, tile.MaxZ),
                new(tile.MaxX, tile.PosY, tile.MaxZ),
                new((tile.MinX + tile.MaxX) * 0.5f, tile.PosY, (tile.MinZ + tile.MaxZ) * 0.5f),
            };

            string[] labels = { "minX_minZ", "maxX_minZ", "minX_maxZ", "maxX_maxZ", "center" };
            for (int i = 0; i < corners.Length; i++)
                samples.Samples.Add(Sample(panel, sceneName, tile.Stem, labels[i], corners[i]));
        }

        // Highest heightmap resolution, not largest area: TLD's flat water/ice tiles can cover
        // more ground than the playable land (CoastalRegion's water is 3800x3000 at res 513 vs
        // Terrain_CoastalMain at 2671x2671, res 2049), and the samples must frame the land.
        TerrainTileBounds? main = tiles
            .OrderByDescending(t => t.HeightmapResolution)
            .ThenByDescending(t => t.SizeX * t.SizeZ)
            .FirstOrDefault();
        if (main != null)
        {
            const int grid = 5;
            for (int iz = 0; iz < grid; iz++)
            {
                for (int ix = 0; ix < grid; ix++)
                {
                    float tx = ix / (float)(grid - 1);
                    float tz = iz / (float)(grid - 1);
                    var world = new Vector3(
                        Mathf.Lerp(main.MinX, main.MaxX, tx),
                        main.PosY,
                        Mathf.Lerp(main.MinZ, main.MaxZ, tz));
                    samples.Samples.Add(Sample(panel, sceneName, main.Stem, $"grid_{ix}_{iz}", world));
                }
            }
        }

        // Round-trip a couple of points if API allows
        try
        {
            var probe = new Vector3(main?.MinX ?? 0f, main?.PosY ?? 0f, main?.MinZ ?? 0f);
            Vector3 map = panel.WorldPositionToMapPosition(sceneName, probe);
            Vector3 worldBack = default;
            float worldRadius = 0f;
            panel.MapPositionToWorldPosition(sceneName, new Vector2(map.x, map.y), 1f, out worldBack, out worldRadius);
            samples.RoundTrip = new RoundTripCheck
            {
                WorldIn = V3(probe),
                Map = V3(map),
                WorldOut = V3(worldBack),
                DeltaMeters = Vector3.Distance(
                    new Vector3(probe.x, 0f, probe.z),
                    new Vector3(worldBack.x, 0f, worldBack.z)),
            };
        }
        catch (Exception ex)
        {
            MelonLogger.Warning($"Map round-trip sample failed: {ex.Message}");
        }

        File.WriteAllText(
            Path.Combine(outDir, "alignment_samples.json"),
            JsonSerializer.Serialize(samples, JsonOpts),
            new UTF8Encoding(encoderShouldEmitUTF8Identifier: false));

        MelonLogger.Msg($"Wrote {samples.Samples.Count} world→map alignment samples");
    }

    private static AlignmentSample Sample(Panel_Map panel, string scene, string tile, string label, Vector3 world)
    {
        Vector3 map = default;
        string? error = null;
        try
        {
            map = panel.WorldPositionToMapPosition(scene, world);
        }
        catch (Exception ex)
        {
            error = ex.Message;
        }

        return new AlignmentSample
        {
            Tile = tile,
            Label = label,
            World = V3(world),
            Map = error == null ? V3(map) : null,
            Error = error,
        };
    }

    private static Panel_Map? TryGetPanelMap()
    {
        try
        {
            return InterfaceManager.GetPanel<Panel_Map>();
        }
        catch (Exception ex)
        {
            MelonLogger.Warning($"GetPanel<Panel_Map> failed: {ex.Message}");
            return null;
        }
    }

    private static FogOfWar? FindFogOfWar()
    {
        try
        {
            var found = UnityEngine.Object.FindObjectsOfType<FogOfWar>(true);
            if (found == null || found.Count == 0)
                return null;

            for (int i = 0; i < found.Count; i++)
            {
                FogOfWar? f = found[i];
                if (f != null)
                    return f;
            }
        }
        catch (Exception ex)
        {
            MelonLogger.Warning($"FindObjectsOfType<FogOfWar> failed: {ex.Message}");
        }

        return null;
    }

    private static readonly JsonSerializerOptions JsonOpts = new()
    {
        WriteIndented = true,
        DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull,
    };

    private static Vec2Dto V2(Vector2 v) => new(v.x, v.y);
    private static Vec3Dto V3(Vector3 v) => new(v.x, v.y, v.z);

    internal sealed class TerrainTileBounds
    {
        public string Stem = "";
        public float MinX;
        public float MaxX;
        public float MinZ;
        public float MaxZ;
        public float PosY;
        public float SizeX;
        public float SizeZ;
        public int HeightmapResolution;
    }

    private sealed class FogOfWarDumpFile
    {
        [JsonPropertyName("formatVersion")] public int FormatVersion { get; set; }
        [JsonPropertyName("sceneName")] public string SceneName { get; set; } = "";
        [JsonPropertyName("dumpedAtUtc")] public string DumpedAtUtc { get; set; } = "";
        [JsonPropertyName("notes")] public string[]? Notes { get; set; }
        [JsonPropertyName("panelMapFound")] public bool PanelMapFound { get; set; }
        [JsonPropertyName("fogOfWarFound")] public bool FogOfWarFound { get; set; }
        [JsonPropertyName("fogOfWarDictionaryKey")] public string? FogOfWarDictionaryKey { get; set; }
        [JsonPropertyName("fogOfWarDictionaryKeys")] public List<string>? FogOfWarDictionaryKeys { get; set; }
        [JsonPropertyName("mapNameOfCurrentScene")] public string? MapNameOfCurrentScene { get; set; }
        [JsonPropertyName("mapNameOfScene")] public string? MapNameOfScene { get; set; }
        [JsonPropertyName("mapRadiusConstant")] public float? MapRadiusConstant { get; set; }
        [JsonPropertyName("fogOfWarRadiusMultiplier")] public float? FogOfWarRadiusMultiplier { get; set; }
        [JsonPropertyName("mapBackgroundFilename")] public string? MapBackgroundFilename { get; set; }
        [JsonPropertyName("mapDetailsFilename")] public string? MapDetailsFilename { get; set; }
        [JsonPropertyName("heightmapFilename")] public string? HeightmapFilename { get; set; }
        [JsonPropertyName("detailScale")] public Vec2Dto? DetailScale { get; set; }
        [JsonPropertyName("heightmapOffset")] public Vec2Dto? HeightmapOffset { get; set; }
        [JsonPropertyName("heightmapScale")] public Vec2Dto? HeightmapScale { get; set; }
        [JsonPropertyName("terrainPositionY")] public float? TerrainPositionY { get; set; }
        [JsonPropertyName("terrainMaxHeight")] public float? TerrainMaxHeight { get; set; }
        [JsonPropertyName("visibleHeightScalar")] public float? VisibleHeightScalar { get; set; }
        [JsonPropertyName("fadeDistance")] public float? FadeDistance { get; set; }
        [JsonPropertyName("sceneRadius")] public float? SceneRadius { get; set; }
        [JsonPropertyName("mapRadius")] public float? MapRadius { get; set; }
        [JsonPropertyName("mapBackgroundWidth")] public int? MapBackgroundWidth { get; set; }
        [JsonPropertyName("mapBackgroundHeight")] public int? MapBackgroundHeight { get; set; }
        [JsonPropertyName("mapDetailsWidth")] public int? MapDetailsWidth { get; set; }
        [JsonPropertyName("mapDetailsHeight")] public int? MapDetailsHeight { get; set; }
        [JsonPropertyName("heightmapWidth")] public int? HeightmapWidth { get; set; }
        [JsonPropertyName("heightmapHeight")] public int? HeightmapHeight { get; set; }
    }

    private sealed class AlignmentSamplesFile
    {
        [JsonPropertyName("formatVersion")] public int FormatVersion { get; set; }
        [JsonPropertyName("sceneName")] public string SceneName { get; set; } = "";
        [JsonPropertyName("mapSpaceNotes")] public string[]? MapSpaceNotes { get; set; }
        [JsonPropertyName("samples")] public List<AlignmentSample> Samples { get; set; } = new();
        [JsonPropertyName("roundTrip")] public RoundTripCheck? RoundTrip { get; set; }
    }

    private sealed class AlignmentSample
    {
        [JsonPropertyName("tile")] public string Tile { get; set; } = "";
        [JsonPropertyName("label")] public string Label { get; set; } = "";
        [JsonPropertyName("world")] public Vec3Dto? World { get; set; }
        [JsonPropertyName("map")] public Vec3Dto? Map { get; set; }
        [JsonPropertyName("error")] public string? Error { get; set; }
    }

    private sealed class RoundTripCheck
    {
        [JsonPropertyName("worldIn")] public Vec3Dto? WorldIn { get; set; }
        [JsonPropertyName("map")] public Vec3Dto? Map { get; set; }
        [JsonPropertyName("worldOut")] public Vec3Dto? WorldOut { get; set; }
        [JsonPropertyName("deltaMeters")] public float DeltaMeters { get; set; }
    }

    private sealed class Vec2Dto
    {
        public Vec2Dto(float x, float y) { X = x; Y = y; }
        [JsonPropertyName("x")] public float X { get; set; }
        [JsonPropertyName("y")] public float Y { get; set; }
    }

    private sealed class Vec3Dto
    {
        public Vec3Dto(float x, float y, float z) { X = x; Y = y; Z = z; }
        [JsonPropertyName("x")] public float X { get; set; }
        [JsonPropertyName("y")] public float Y { get; set; }
        [JsonPropertyName("z")] public float Z { get; set; }
    }
}
