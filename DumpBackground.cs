using MelonLoader;
using UnityEngine;

namespace TerrainDumper;

/// <summary>
/// Keeps Unity's player loop alive while dumps run (unfocused / minimized).
/// Ref-counted so nested dump_map → enrichment → ortho restores correctly.
/// </summary>
internal static class DumpBackground
{
    private static int _holders;
    private static bool _savedRunInBackground;

    public static void Acquire()
    {
        if (_holders++ == 0)
        {
            _savedRunInBackground = Application.runInBackground;
            Application.runInBackground = true;
            MelonLogger.Msg("Dump background run ON (Unity keeps ticking while unfocused/minimized).");
        }
    }

    public static void Release()
    {
        if (_holders <= 0)
            return;

        if (--_holders == 0)
        {
            Application.runInBackground = _savedRunInBackground;
            MelonLogger.Msg($"Dump background run restored (runInBackground={_savedRunInBackground}).");
        }
    }
}
