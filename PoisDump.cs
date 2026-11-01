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
/// Dump DisplayLocationLabel + MapDetail world positions for template stamping (named POIs).
/// Clutter (harvestables / corpses / containers) is skipped at dump time.
/// </summary>
internal static class PoisDump
{
    private const int FormatVersion = 1;

    // Keep in sync with tools/stamp_template_portals.py --with-pois filters.
    private static readonly string[] SkipSprites =
    {
        "icoMap_corpse", "icoMap_deerCarcass", "icoMap_wolfCarcass", "icoMap_bearCarcass",
        "icoMap_CougarCarcass", "icoMap_container", "ico_Radial_pack",
        "icoMap_cattails", "icoMap_rosehips", "icoMap_oldmansbeard", "icoMap_reishi",
        "icoMap_sapling", "icoMap_burdock", "icoMap_limb", "icoMap_ptarmiganNest",
        "icoMap_coal", "icoMap_bark", "icoMap_rabbit", "icoMap_car",
        "ico_collections_polaroids",
    };

    private static readonly string[] SkipLocPrefixes =
    {
        "GAMEPLAY_BackPack", "GAMEPLAY_Corpse", "GAMEPLAY_FrozenCorpse",
        "GAMEPLAY_DeerCarcass", "GAMEPLAY_WolfCarcass",
        "GAMEPLAY_MetalContainer", "GAMEPLAY_PlasticContainer", "GAMEPLAY_DecorationBox",
        "GAMEPLAY_Climb", "GAMEPLAY_Cattail", "GAMEPLAY_RoseHip", "GAMEPLAY_OldMansBeard",
        "GAMEPLAY_BirchSapling", "GAMEPLAY_MapleSapling", "GAMEPLAY_Burdock",
        "GAMEPLAY_Reishi", "GAMEPLAY_RabbitGrove", "GAMEPLAY_Vehicle", "GAMEPLAY_VisorNote",
    };

    private static readonly string[] KeepSprites =
    {
        "icoMap_cave", "icoMap_transitions", "map_transition_map",
        "icoMap_mine", "icoMap_hatch", "icoMap_location", "icoMap_Generic",
    };

    public static void RunFromConsole() => Run();

    public static void Run()
    {
        try
        {
            Dump();
        }
        catch (Exception ex)
        {
            MelonLogger.Error($"dump_pois failed: {ex}");
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

        var pois = new List<PoiEntry>();
        int skippedClutter = 0;
        int locationLabelCount = 0;
        int mapDetailCount = 0;

        DisplayLocationLabel[]? labels = null;
        try { labels = UnityEngine.Object.FindObjectsOfType<DisplayLocationLabel>(true); }
        catch (Exception ex)
        {
            MelonLogger.Warning($"FindObjectsOfType<DisplayLocationLabel> failed: {ex.Message}");
        }

        if (labels != null)
        {
            foreach (DisplayLocationLabel lab in labels)
            {
                if (lab == null)
                    continue;
                locationLabelCount++;

                string locId = ReadLocationLocId(lab);
                string goName = "";
                Vector3 pos;
                try
                {
                    Transform tr = lab.transform;
                    pos = tr.position;
                    goName = lab.gameObject != null ? lab.gameObject.name : "";
                }
                catch
                {
                    continue;
                }

                if (IsClutter(locId, sprite: null, goName))
                {
                    skippedClutter++;
                    continue;
                }

                pois.Add(new PoiEntry
                {
                    Kind = "location",
                    LocId = string.IsNullOrWhiteSpace(locId) ? null : locId.Trim(),
                    GameObject = string.IsNullOrWhiteSpace(goName) ? null : goName,
                    X = pos.x,
                    Y = pos.y,
                    Z = pos.z,
                });
            }
        }

        MapDetail[]? details = null;
        try { details = UnityEngine.Object.FindObjectsOfType<MapDetail>(true); }
        catch (Exception ex)
        {
            MelonLogger.Warning($"FindObjectsOfType<MapDetail> failed: {ex.Message}");
        }

        if (details != null)
        {
            foreach (MapDetail md in details)
            {
                if (md == null)
                    continue;
                mapDetailCount++;

                string locId = "";
                string sprite = "";
                int? iconType = null;
                float? surveyRadius = null;
                try { locId = md.m_LocID ?? ""; } catch { /* ignore */ }
                try { sprite = md.m_SpriteName ?? ""; } catch { /* ignore */ }
                try { iconType = (int)md.m_IconType; } catch { /* ignore */ }
                try { surveyRadius = md.m_SurveyRadius; } catch { /* ignore */ }

                string goName = "";
                Vector3 pos;
                try
                {
                    Transform tr = md.transform;
                    pos = tr.position;
                    goName = md.gameObject != null ? md.gameObject.name : "";
                }
                catch
                {
                    continue;
                }

                if (goName.StartsWith("FX_", StringComparison.OrdinalIgnoreCase)
                    && !IsKeepSprite(sprite))
                {
                    skippedClutter++;
                    continue;
                }

                if (IsClutter(locId, sprite, goName))
                {
                    skippedClutter++;
                    continue;
                }

                pois.Add(new PoiEntry
                {
                    Kind = "mapdetail",
                    LocId = string.IsNullOrWhiteSpace(locId) ? null : locId.Trim(),
                    Sprite = string.IsNullOrWhiteSpace(sprite) ? null : sprite.Trim(),
                    IconType = iconType,
                    SurveyRadius = surveyRadius,
                    GameObject = string.IsNullOrWhiteSpace(goName) ? null : goName,
                    X = pos.x,
                    Y = pos.y,
                    Z = pos.z,
                });
            }
        }

        // Prefer mapdetail over location when both share locId + ~20m cell.
        pois = Dedupe(pois);

        pois.Sort((a, b) =>
        {
            int c = string.CompareOrdinal(a.Kind, b.Kind);
            if (c != 0) return c;
            c = string.CompareOrdinal(a.LocId ?? "", b.LocId ?? "");
            if (c != 0) return c;
            c = a.X.CompareTo(b.X);
            return c != 0 ? c : a.Z.CompareTo(b.Z);
        });

        Vector3? playerPos = null;
        try
        {
            Transform? pt = GameManager.GetPlayerTransform();
            if (pt != null)
                playerPos = pt.position;
        }
        catch { /* ignore */ }

        var doc = new PoiDoc
        {
            FormatVersion = FormatVersion,
            SceneName = sceneName,
            DumpedAtUtc = DateTime.UtcNow.ToString("o", CultureInfo.InvariantCulture),
            ModVersion = Implementation.ModVersion,
            Method = "FindObjectsOfType<DisplayLocationLabel> + FindObjectsOfType<MapDetail>",
            LocationLabelCount = locationLabelCount,
            MapDetailCount = mapDetailCount,
            PoiCount = pois.Count,
            SkippedClutter = skippedClutter,
            PlayerX = playerPos?.x,
            PlayerY = playerPos?.y,
            PlayerZ = playerPos?.z,
            Pois = pois,
            Notes = new[]
            {
                "pois[]: named charcoal locations (DisplayLocationLabel) + MapDetail icons.",
                "x/y/z: world position of the component GameObject.",
                "Clutter (harvestables/corpses/containers) skipped via sprite/locId lists.",
                "Deduped by (locId, ~20m xz); mapdetail preferred over location.",
            },
        };

        string path = Path.Combine(outDir, "pois.json");
        File.WriteAllText(
            path,
            JsonSerializer.Serialize(doc, JsonOpts),
            new UTF8Encoding(encoderShouldEmitUTF8Identifier: false));

        Msg($"POIs done → {path} (labels={locationLabelCount}, mapDetails={mapDetailCount}, kept={pois.Count}, skippedClutter={skippedClutter})");
    }

    private static string ReadLocationLocId(DisplayLocationLabel lab)
    {
        try
        {
            var localized = lab.m_LocalizedLabel;
            if (localized != null)
            {
                string? id = localized.m_LocalizationID;
                if (!string.IsNullOrWhiteSpace(id))
                    return id;
            }
        }
        catch { /* ignore */ }
        return "";
    }

    private static bool IsKeepSprite(string? sprite)
    {
        if (string.IsNullOrWhiteSpace(sprite))
            return false;
        foreach (string k in KeepSprites)
        {
            if (string.Equals(sprite, k, StringComparison.OrdinalIgnoreCase))
                return true;
        }
        return false;
    }

    private static bool IsClutter(string? locId, string? sprite, string? goName)
    {
        if (!string.IsNullOrWhiteSpace(sprite) && !IsKeepSprite(sprite))
        {
            foreach (string s in SkipSprites)
            {
                if (string.Equals(sprite, s, StringComparison.OrdinalIgnoreCase))
                    return true;
            }
        }

        if (!string.IsNullOrWhiteSpace(locId))
        {
            foreach (string p in SkipLocPrefixes)
            {
                if (locId.StartsWith(p, StringComparison.OrdinalIgnoreCase))
                    return true;
            }
        }

        if (!string.IsNullOrWhiteSpace(goName) && !IsKeepSprite(sprite))
        {
            string g = goName;
            if (g.StartsWith("GAMEPLAY_Corpse", StringComparison.OrdinalIgnoreCase)
                || g.StartsWith("GAMEPLAY_BackPack", StringComparison.OrdinalIgnoreCase)
                || g.StartsWith("GAMEPLAY_Cattail", StringComparison.OrdinalIgnoreCase)
                || g.StartsWith("GAMEPLAY_RoseHip", StringComparison.OrdinalIgnoreCase)
                || g.IndexOf("Carcass", StringComparison.OrdinalIgnoreCase) >= 0)
                return true;
        }

        return false;
    }

    private static List<PoiEntry> Dedupe(List<PoiEntry> pois)
    {
        const float cell = 20f;
        var best = new Dictionary<string, PoiEntry>();
        foreach (PoiEntry p in pois)
        {
            int qx = (int)MathF.Floor(p.X / cell);
            int qz = (int)MathF.Floor(p.Z / cell);
            string key = $"{p.LocId ?? ""}|{qx}|{qz}";
            if (!best.TryGetValue(key, out PoiEntry? prev))
            {
                best[key] = p;
                continue;
            }
            // Prefer mapdetail (has sprite) over bare location label.
            bool preferNew = prev.Kind == "location" && p.Kind == "mapdetail";
            if (preferNew)
                best[key] = p;
        }
        return best.Values.ToList();
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

    private sealed class PoiDoc
    {
        [JsonPropertyName("formatVersion")] public int FormatVersion { get; set; }
        [JsonPropertyName("sceneName")] public string SceneName { get; set; } = "";
        [JsonPropertyName("dumpedAtUtc")] public string DumpedAtUtc { get; set; } = "";
        [JsonPropertyName("modVersion")] public string ModVersion { get; set; } = "";
        [JsonPropertyName("method")] public string Method { get; set; } = "";
        [JsonPropertyName("locationLabelCount")] public int LocationLabelCount { get; set; }
        [JsonPropertyName("mapDetailCount")] public int MapDetailCount { get; set; }
        [JsonPropertyName("poiCount")] public int PoiCount { get; set; }
        [JsonPropertyName("skippedClutter")] public int SkippedClutter { get; set; }
        [JsonPropertyName("playerX")] public float? PlayerX { get; set; }
        [JsonPropertyName("playerY")] public float? PlayerY { get; set; }
        [JsonPropertyName("playerZ")] public float? PlayerZ { get; set; }
        [JsonPropertyName("pois")] public List<PoiEntry> Pois { get; set; } = new();
        [JsonPropertyName("notes")] public string[]? Notes { get; set; }
    }

    private sealed class PoiEntry
    {
        [JsonPropertyName("kind")] public string Kind { get; set; } = "";
        [JsonPropertyName("locId")] public string? LocId { get; set; }
        [JsonPropertyName("sprite")] public string? Sprite { get; set; }
        [JsonPropertyName("iconType")] public int? IconType { get; set; }
        [JsonPropertyName("surveyRadius")] public float? SurveyRadius { get; set; }
        [JsonPropertyName("gameObject")] public string? GameObject { get; set; }
        [JsonPropertyName("x")] public float X { get; set; }
        [JsonPropertyName("y")] public float Y { get; set; }
        [JsonPropertyName("z")] public float Z { get; set; }
    }
}
