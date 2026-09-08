<#
.SYNOPSIS
Build a vintage-color map_bg PNG for one region from its TerrainDumper dump.

.DESCRIPTION
Same dump/mask checks as build_region_map.ps1, but runs make_map_bg.py with
--pipeline color (partial chroma, soft levels, cool dim-gray edge). Does not
replace the charcoal/legacy ship path. Writes map_bg_<Scene>_color.png under
out\maps_color (or -OutDir).

.EXAMPLE
./tools/build_region_map_color.ps1 LongRailTransitionZone

.EXAMPLE
./tools/build_region_map_color.ps1 AshCanyonRegion -Size 8192

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
Build: ./tools/build_region_map_color.ps1 BlackrockRegion
(Or call make_map_bg.py directly only for debug.)
"@
}

$dump = Join-Path $DumpRoot $Scene
if (-not (Test-Path $dump)) {
    throw "No dump for '$Scene' at $dump. Run dump_map in that region first."
}
foreach ($required in @('alignment_samples.json', 'fog_of_war.json', 'meta.json')) {
    if (-not (Test-Path (Join-Path $dump $required))) {
        throw "Dump at $dump is missing $required. Open the charcoal map once in region, then re-run dump_map."
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

if (-not $OutDir) { $OutDir = Join-Path $repo 'out\maps_color' }
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$out = Join-Path $OutDir "map_bg_${Scene}_color.png"

$argList = @($script, $dump, '--size', $Size, '--pipeline', 'color', '--out', $out)
if ($Baseline) { $argList += @('--baseline', $Baseline) }
if ($MakeMapBgArgs) { $argList += $MakeMapBgArgs }

Write-Host "build_region_map_color: $Scene -> $out" -ForegroundColor Cyan
& python @argList
if ($LASTEXITCODE -ne 0) {
    throw "make_map_bg failed (exit $LASTEXITCODE)"
}

Write-Host "wrote $out" -ForegroundColor Green
Write-Host 'Deploy manually: copy into DetailedMaps\Maps if using a color map set'
