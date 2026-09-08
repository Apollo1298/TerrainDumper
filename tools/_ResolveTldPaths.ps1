# Shared path resolution for TerrainDumper tooling.
# Dot-source from other scripts: . (Join-Path $PSScriptRoot '_ResolveTldPaths.ps1')
#
# Env:
#   TLD_PATH              Game install (.../TheLongDark)
#   TERRAIN_DUMPER_ROOT   Dump folder (.../Mods/TerrainDumper); optional if TLD_PATH set

function Get-TerrainDumperRoot {
    param(
        [string]$DumpRoot
    )

    if ($DumpRoot) {
        return $DumpRoot.TrimEnd('\', '/')
    }
    if ($env:TERRAIN_DUMPER_ROOT) {
        return $env:TERRAIN_DUMPER_ROOT.TrimEnd('\', '/')
    }
    if ($env:TLD_PATH) {
        return Join-Path $env:TLD_PATH.TrimEnd('\', '/') 'Mods\TerrainDumper'
    }

    throw @"
Set environment variable TLD_PATH to your The Long Dark install folder
(e.g. ...\steamapps\common\TheLongDark), or TERRAIN_DUMPER_ROOT to the
Mods\TerrainDumper dump folder, or pass -DumpRoot.
"@
}

function Get-TldPath {
    param(
        [string]$GamePath
    )

    if ($GamePath) {
        return $GamePath.TrimEnd('\', '/')
    }
    if ($env:TLD_PATH) {
        return $env:TLD_PATH.TrimEnd('\', '/')
    }

    throw @"
Set environment variable TLD_PATH to your The Long Dark install folder
(e.g. ...\steamapps\common\TheLongDark), or pass -GamePath.
"@
}
