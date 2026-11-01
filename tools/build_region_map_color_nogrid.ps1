<#
.SYNOPSIS
Build a vintage-color map_bg PNG with no world gridlines.

.DESCRIPTION
Same dump/mask checks as build_region_map_color.ps1, but passes --no-grid to
make_map_bg.py. Writes map_bg_<Scene>_color.png under out\maps_color_nogrid
(or -OutDir).

.EXAMPLE
./tools/build_region_map_color_nogrid.ps1 LongRailTransitionZone

.EXAMPLE
./tools/build_region_map_color_nogrid.ps1 AshCanyonRegion -Size 8192

Requires TLD_PATH or TERRAIN_DUMPER_ROOT (or -DumpRoot).
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [string]$Scene,

    [int]$Size = 4096,

    [string]$DumpRoot = '',

    [string]$OutDir,

    [string]$Baseline,

    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$MakeMapBgArgs
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '_ResolveTldPaths.ps1')
$DumpRoot = Get-TerrainDumperRoot -DumpRoot $DumpRoot

$repo = Split-Path -Parent $PSScriptRoot
$script = Join-Path $PSScriptRoot 'make_map_bg.py'
if (-not (Test-Path $script)) {
    throw "Missing $script"
}

if ($Scene -eq 'BlackrockPrisonSurvivalZone') {
    throw @"
'$Scene' is not a final map region — it is composited into BlackrockRegion.
Build: ./tools/build_region_map_color_nogrid.ps1 BlackrockRegion
(Or call make_map_bg.py directly only for debug.)
"@
}

$dump = Join-Path $DumpRoot $Scene
if (-not (Test-Path $dump)) {
    throw "No dump for '$Scene' at $dump. Run dump_map in that region first."
}

$isMod = $Scene.StartsWith('Mod') -and -not $Scene.EndsWith('_Region')
$fowDump = Join-Path $dump 'fow.json'
$fowMask = Join-Path $repo "masks\$Scene\fow.json"
$hasFow = (Test-Path $fowDump) -or (Test-Path $fowMask)

if ($isMod) {
    if (-not $hasFow) {
        throw @"
Mod scene '$Scene' needs fow.json for map framing.
  Expected: $fowDump
         or $fowMask
  Generate: python tools/make_fow_config.py `"$dump`"
"@
    }
    foreach ($required in @('meta.json')) {
        if (-not (Test-Path (Join-Path $dump $required))) {
            throw "Dump at $dump is missing $required."
        }
    }
}
else {
    foreach ($required in @('alignment_samples.json', 'fog_of_war.json', 'meta.json')) {
        if (-not (Test-Path (Join-Path $dump $required))) {
            throw "Dump at $dump is missing $required. Open the charcoal map once in region, then re-run dump_map."
        }
    }
}
if (-not (Get-ChildItem -Path $dump -Filter 'terrain_*_meta.json' -File)) {
    throw "Dump at $dump has no terrain tiles. Re-run dump_map in that region."
}

$maskPng = Join-Path $repo "masks\$Scene\mask.png"
$maskJson = Join-Path $repo "masks\$Scene\mask.json"
if (-not (Test-Path $maskPng) -or -not (Test-Path $maskJson)) {
    throw @"
Missing exclusion mask for '$Scene'.
  Expected: masks\$Scene\mask.png + mask.json
  Create template: python tools/make_mask_template.py `"$dump`"
  Paint exclude areas, export as masks\$Scene\mask.png
"@
}

if (-not $OutDir) { $OutDir = Join-Path $repo 'out\maps_color_nogrid' }
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$out = Join-Path $OutDir "map_bg_${Scene}_color.png"

$argList = @($script, $dump, '--size', $Size, '--pipeline', 'color', '--no-grid', '--out', $out)
if ($Baseline) { $argList += @('--baseline', $Baseline) }
if ($isMod) {
    $argList += '--from-fow'
    if (Test-Path $fowDump) {
        $argList += @('--fow-config', $fowDump)
    }
    elseif (Test-Path $fowMask) {
        $argList += @('--fow-config', $fowMask)
    }
}
if ($MakeMapBgArgs) { $argList += $MakeMapBgArgs }

Write-Host "build_region_map_color_nogrid: $Scene -> $out" -ForegroundColor Cyan
if ($isMod) {
    Write-Host "  Mod framing: fow.json (not fog_of_war.json / stale alignment)" -ForegroundColor DarkCyan
}
& python @argList
if ($LASTEXITCODE -ne 0) {
    throw "make_map_bg failed (exit $LASTEXITCODE)"
}

Write-Host "wrote $out" -ForegroundColor Green
Write-Host 'Deploy manually: copy into DetailedMaps\Maps if using a no-grid color set'
