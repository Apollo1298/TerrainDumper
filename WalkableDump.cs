using System.Globalization;
using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;
using MelonLoader;
using MelonLoader.Utils;
using UnityEngine;
using UnityEngine.AI;
using UnitySceneManager = UnityEngine.SceneManagement.SceneManager;

namespace TerrainDumper;

/// <summary>
/// Dump baked NavMesh triangulation and rasterize coverage onto an XZ grid
/// (terrain AABB). Replaces raycast + SamplePosition probing.
/// </summary>
internal static class WalkableDump
{
    private const float DefaultCellSize = 4f;
    private const float MinCellSize = 1f;
    private const float MaxCellSize = 50f;
    private const int FormatVersion = 2;

    private static bool _running;

    public static bool IsRunning => _running;

    public static void Run(float cellSize = DefaultCellSize, bool rasterize = true)
    {
        if (_running)
        {
            Msg("Walkable dump already in progress…");
            return;
        }

        try
        {
            if (!float.IsFinite(cellSize) || cellSize < MinCellSize || cellSize > MaxCellSize)
            {
                Msg($"dump_walkable: cell size must be {MinCellSize}..{MaxCellSize} m (got {cellSize}). Usage: dump_walkable [cellMeters] [rasterize 0|1]");
                return;
            }

            Dump(cellSize, rasterize);
        }
        catch (Exception ex)
        {
            MelonLogger.Error($"dump_walkable failed: {ex}");
            try { Il2Cpp.uConsoleLog.Add($"TerrainDumper ERROR: {ex.Message}"); } catch { /* ignore */ }
            _running = false;
            DumpBackground.Release();
        }
    }

    private static void Dump(float cellSize, bool rasterize)
    {
        Terrain[] terrains = Terrain.activeTerrains;
        if (terrains == null || terrains.Length == 0)
            throw new InvalidOperationException("No active Terrains — load a region first.");

        if (!TryTerrainBounds(terrains, out float minX, out float maxX, out float minZ, out float maxZ, out float minY, out float maxY))
            throw new InvalidOperationException("Could not compute terrain bounds (all active Terrains were scaled backdrops).");

        string sceneName = UnitySceneManager.GetActiveScene().name;
        if (string.IsNullOrWhiteSpace(sceneName))
            sceneName = "UnknownScene";

        string outDir = Path.Combine(MelonEnvironment.ModsDirectory, "TerrainDumper", sceneName);
        Directory.CreateDirectory(outDir);

        _running = true;
        DumpBackground.Acquire();

        Msg("Walkable: calculating NavMesh triangulation…");
        NavMeshTriangulation tri = NavMesh.CalculateTriangulation();
        var srcVerts = tri.vertices;
        var srcIndices = tri.indices;
        int vertCount = srcVerts != null ? srcVerts.Length : 0;
        int indexCount = srcIndices != null ? srcIndices.Length : 0;
        if (vertCount < 3 || indexCount < 3 || srcVerts == null || srcIndices == null)
            throw new InvalidOperationException(
                $"NavMesh.CalculateTriangulation returned empty mesh (verts={vertCount}, indices={indexCount}). Is NavMesh loaded?");

        // Copy out of Il2Cpp arrays into managed buffers.
        var verts = new Vector3[vertCount];
        for (int i = 0; i < vertCount; i++)
            verts[i] = srcVerts[i];

        var indices = new int[indexCount];
        for (int i = 0; i < indexCount; i++)
            indices[i] = srcIndices[i];

        int triCount = indexCount / 3;
        Msg($"Walkable: mesh verts={vertCount} tris={triCount} — writing raw" + (rasterize ? $" + rasterizing @ {cellSize}m…" : " (raster skipped)…"));

        // Binary mesh: verts as LE float32 xyz; indices as LE int32.
        const string vertsName = "navmesh_vertices.raw";
        const string indicesName = "navmesh_indices.raw";
        WriteVerticesRaw(Path.Combine(outDir, vertsName), verts);
        WriteIndicesRaw(Path.Combine(outDir, indicesName), indices);

        int width = 0, height = 0, covered = 0;
        string? maskName = null;
        if (rasterize)
        {
            width = Math.Max(2, Mathf.FloorToInt((maxX - minX) / cellSize) + 1);
            height = Math.Max(2, Mathf.FloorToInt((maxZ - minZ) / cellSize) + 1);
            var mask = new byte[width * height];
            covered = Rasterize(mask, width, height, minX, minZ, cellSize, verts, indices);
            maskName = "walkable_mask.raw";
            File.WriteAllBytes(Path.Combine(outDir, maskName), mask);
        }

        var meta = new WalkableMeta
        {
            FormatVersion = FormatVersion,
            SceneName = sceneName,
            DumpedAtUtc = DateTime.UtcNow.ToString("o", CultureInfo.InvariantCulture),
            ModVersion = Implementation.ModVersion,
            Method = rasterize
                ? "NavMesh.CalculateTriangulation + XZ rasterize"
                : "NavMesh.CalculateTriangulation (mesh only)",
            CellSize = cellSize,
            Rasterized = rasterize,
            Width = width,
            Height = height,
            OriginX = minX,
            OriginZ = minZ,
            SizeX = rasterize ? (width - 1) * cellSize : maxX - minX,
            SizeZ = rasterize ? (height - 1) * cellSize : maxZ - minZ,
            BoundsMinY = minY,
            BoundsMaxY = maxY,
            VertexCount = vertCount,
            TriangleCount = triCount,
            CoveredCellCount = covered,
            CellCount = rasterize ? width * height : 0,
            MaskFile = maskName,
            VerticesFile = vertsName,
            IndicesFile = indicesName,
            Notes = new[]
            {
                "navmesh_vertices.raw: LE float32 xyz per vertex.",
                "navmesh_indices.raw: LE int32 triangle indices (len = triangleCount*3).",
                "Grid footprint = active Terrain AABB (same as enrichment) when rasterized.",
                "mask byte 1 = cell center lies inside at least one NavMesh triangle (XZ projection).",
                "Row-major [z, x]; world X = originX + ix*cellSize, Z = originZ + iz*cellSize.",
                "Pathfinding should prefer the triangle mesh; raster is optional coverage preview.",
                "Also run dump_portals (auto after walkable) for LoadScene edges.",
            },
        };

        File.WriteAllText(
            Path.Combine(outDir, "walkable_meta.json"),
            JsonSerializer.Serialize(meta, JsonOpts),
            new UTF8Encoding(encoderShouldEmitUTF8Identifier: false));

        if (rasterize)
        {
            float pct = 100f * covered / Math.Max(1, width * height);
            Msg($"Walkable done → {outDir} (covered={covered}/{width * height} = {pct:F1}%, tris={triCount})");
        }
        else
        {
            Msg($"Walkable done → {outDir} (mesh only, tris={triCount}, verts={vertCount})");
        }

        _running = false;
        DumpBackground.Release();

        // Portal graph seeds for the same scene.
        try { PortalDump.Run(); }
        catch (Exception ex) { MelonLogger.Warning($"Auto dump_portals after walkable failed: {ex.Message}"); }
    }

    private static bool TryTerrainBounds(
        Terrain[] terrains,
        out float minX, out float maxX,
        out float minZ, out float maxZ,
        out float minY, out float maxY)
    {
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

        return float.IsFinite(minX);
    }

    private static void WriteVerticesRaw(string path, Vector3[] verts)
    {
        var buf = new byte[verts.Length * 12];
        for (int i = 0; i < verts.Length; i++)
        {
            int o = i * 12;
            WriteF32(buf, o, verts[i].x);
            WriteF32(buf, o + 4, verts[i].y);
            WriteF32(buf, o + 8, verts[i].z);
        }
        File.WriteAllBytes(path, buf);
    }

    private static void WriteIndicesRaw(string path, int[] indices)
    {
        var buf = new byte[indices.Length * 4];
        for (int i = 0; i < indices.Length; i++)
            WriteI32(buf, i * 4, indices[i]);
        File.WriteAllBytes(path, buf);
    }

    private static void WriteF32(byte[] buf, int offset, float v)
    {
        var b = BitConverter.GetBytes(v);
        if (!BitConverter.IsLittleEndian)
            Array.Reverse(b);
        Buffer.BlockCopy(b, 0, buf, offset, 4);
    }

    private static void WriteI32(byte[] buf, int offset, int v)
    {
        var b = BitConverter.GetBytes(v);
        if (!BitConverter.IsLittleEndian)
            Array.Reverse(b);
        Buffer.BlockCopy(b, 0, buf, offset, 4);
    }

    /// <summary>Mark cells whose centers fall inside any triangle (XZ). Returns covered cell count.</summary>
    private static int Rasterize(
        byte[] mask, int width, int height,
        float originX, float originZ, float cell,
        Vector3[] verts, int[] indices)
    {
        int covered = 0;
        int triCount = indices.Length / 3;
        for (int t = 0; t < triCount; t++)
        {
            Vector3 a = verts[indices[t * 3]];
            Vector3 b = verts[indices[t * 3 + 1]];
            Vector3 c = verts[indices[t * 3 + 2]];

            float ax = (a.x - originX) / cell;
            float az = (a.z - originZ) / cell;
            float bx = (b.x - originX) / cell;
            float bz = (b.z - originZ) / cell;
            float cx = (c.x - originX) / cell;
            float cz = (c.z - originZ) / cell;

            int minIx = Mathf.Max(0, Mathf.FloorToInt(Mathf.Min(ax, Mathf.Min(bx, cx))));
            int maxIx = Mathf.Min(width - 1, Mathf.CeilToInt(Mathf.Max(ax, Mathf.Max(bx, cx))));
            int minIz = Mathf.Max(0, Mathf.FloorToInt(Mathf.Min(az, Mathf.Min(bz, cz))));
            int maxIz = Mathf.Min(height - 1, Mathf.CeilToInt(Mathf.Max(az, Mathf.Max(bz, cz))));

            for (int iz = minIz; iz <= maxIz; iz++)
            {
                for (int ix = minIx; ix <= maxIx; ix++)
                {
                    int idx = iz * width + ix;
                    if (mask[idx] != 0)
                        continue;
                    float px = ix + 0.5f;
                    float pz = iz + 0.5f;
                    if (!PointInTriangle(px, pz, ax, az, bx, bz, cx, cz))
                        continue;
                    mask[idx] = 1;
                    covered++;
                }
            }

            if ((t + 1) % 50000 == 0)
                MelonLogger.Msg($"Walkable raster {t + 1}/{triCount} tris, covered={covered}…");
        }

        return covered;
    }

    private static bool PointInTriangle(
        float px, float pz,
        float ax, float az, float bx, float bz, float cx, float cz)
    {
        // Barycentric in XZ (same winding as projected triangle).
        float v0x = cx - ax, v0z = cz - az;
        float v1x = bx - ax, v1z = bz - az;
        float v2x = px - ax, v2z = pz - az;
        float dot00 = v0x * v0x + v0z * v0z;
        float dot01 = v0x * v1x + v0z * v1z;
        float dot02 = v0x * v2x + v0z * v2z;
        float dot11 = v1x * v1x + v1z * v1z;
        float dot12 = v1x * v2x + v1z * v2z;
        float denom = dot00 * dot11 - dot01 * dot01;
        if (Mathf.Abs(denom) < 1e-12f)
            return false;
        float inv = 1f / denom;
        float u = (dot11 * dot02 - dot01 * dot12) * inv;
        float v = (dot00 * dot12 - dot01 * dot02) * inv;
        return u >= 0f && v >= 0f && (u + v) <= 1f;
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

    private sealed class WalkableMeta
    {
        [JsonPropertyName("formatVersion")] public int FormatVersion { get; set; }
        [JsonPropertyName("sceneName")] public string SceneName { get; set; } = "";
        [JsonPropertyName("dumpedAtUtc")] public string DumpedAtUtc { get; set; } = "";
        [JsonPropertyName("modVersion")] public string ModVersion { get; set; } = "";
        [JsonPropertyName("method")] public string Method { get; set; } = "";
        [JsonPropertyName("cellSize")] public float CellSize { get; set; }
        [JsonPropertyName("rasterized")] public bool Rasterized { get; set; }
        [JsonPropertyName("width")] public int Width { get; set; }
        [JsonPropertyName("height")] public int Height { get; set; }
        [JsonPropertyName("originX")] public float OriginX { get; set; }
        [JsonPropertyName("originZ")] public float OriginZ { get; set; }
        [JsonPropertyName("sizeX")] public float SizeX { get; set; }
        [JsonPropertyName("sizeZ")] public float SizeZ { get; set; }
        [JsonPropertyName("boundsMinY")] public float BoundsMinY { get; set; }
        [JsonPropertyName("boundsMaxY")] public float BoundsMaxY { get; set; }
        [JsonPropertyName("vertexCount")] public int VertexCount { get; set; }
        [JsonPropertyName("triangleCount")] public int TriangleCount { get; set; }
        [JsonPropertyName("coveredCellCount")] public int CoveredCellCount { get; set; }
        [JsonPropertyName("cellCount")] public int CellCount { get; set; }
        [JsonPropertyName("maskFile")] public string? MaskFile { get; set; }
        [JsonPropertyName("verticesFile")] public string VerticesFile { get; set; } = "";
        [JsonPropertyName("indicesFile")] public string IndicesFile { get; set; } = "";
        [JsonPropertyName("notes")] public string[]? Notes { get; set; }
    }
}
