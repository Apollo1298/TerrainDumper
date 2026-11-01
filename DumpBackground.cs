using MelonLoader;
using UnityEngine;

namespace TerrainDumper;

/// <summary>
/// Keeps Unity's player loop alive while dumps run (unfocused / minimized).
/// Ref-counted so nested dump_map → enrichment → ortho restores correctly.
/// </summary>
/// <remarks>
/// TLD scene loads can clear <see cref="Application.runInBackground"/>. Nested
/// Acquire() used to skip re-asserting the flag — batch dumpers then starved
/// after the first region change. Pulse()/Acquire() always force it back on.
/// </remarks>
internal static class DumpBackground
{
    private static int _holders;
    private static bool _savedRunInBackground;

    public static bool IsHeld => _holders > 0;

    public static void Acquire()
    {
        if (_holders++ == 0)
            _savedRunInBackground = Application.runInBackground;

        // Always re-assert: scene loads / GameManager can clear the flag while we hold.
        Application.runInBackground = true;
        if (_holders == 1)
            MelonLogger.Msg("Dump background run ON (Unity keeps ticking while unfocused/minimized).");
    }

    /// <summary>
    /// Re-assert runInBackground while any dump holds the lock (call from OnUpdate ticks).
    /// </summary>
    public static void Pulse()
    {
        if (_holders <= 0)
            return;
        if (!Application.runInBackground)
            Application.runInBackground = true;
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
