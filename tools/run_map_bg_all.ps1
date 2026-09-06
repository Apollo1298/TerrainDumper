<#
.SYNOPSIS
Run make_map_bg.py for every non-mod region under masks/.

.DESCRIPTION
Scene list = folders in masks/ minus Mod*. Each region still needs a painted
mask.png + mask.json and a complete TerrainDumper dump. Writes
map_bg_<Scene>_new.png under out\maps (or -OutDir).

.EXAMPLE
./tools/run_map_bg_all.ps1

.EXAMPLE
./tools/run_map_bg_all.ps1 -Size 8192

.EXAMPLE
./tools/run_map_bg_all.ps1 RuralRegion TracksRegion
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0, ValueFromRemainingArguments = $true)]
    [string[]]$Scenes,

    [string]$DumpRoot = 'I:\SteamLibrary\steamapps\common\TheLongDark\Mods\TerrainDumper',

    [string]$OutDir,

    [int]$Size = 4096
)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$script = Join-Path $PSScriptRoot 'make_map_bg.py'
if (-not (Test-Path $script)) { throw "Missing $script" }
if (-not (Test-Path $DumpRoot)) { throw "Dump root not found: $DumpRoot" }

if (-not $OutDir) { $OutDir = Join-Path $repo 'out\maps' }
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

$maskRoot = Join-Path $repo 'masks'
if (-not (Test-Path $maskRoot)) { throw "Missing masks folder: $maskRoot" }

function Test-IsNonModScene([string]$name) {
    return -not ($name -like 'Mod*')
}

if ($Scenes -and $Scenes.Count -gt 0) {
    $scenes = @($Scenes | Sort-Object -Unique)
}
else {
    $scenes = @(
        Get-ChildItem $maskRoot -Directory |
            Where-Object { Test-IsNonModScene $_.Name } |
            ForEach-Object { $_.Name } |
            Sort-Object
    )
}

$ok = @()
$skip = @()
$fail = @()

foreach ($scene in $scenes) {
    if (-not (Test-IsNonModScene $scene)) {
        $skip += "$scene (mod)"
        continue
    }

    $dump = Join-Path $DumpRoot $scene
    if (-not (Test-Path $dump)) {
        $skip += "$scene (no dump)"
        continue
    }

    $maskPng = Join-Path $maskRoot "$scene\mask.png"
    $maskJson = Join-Path $maskRoot "$scene\mask.json"
    $need = @(
        (Join-Path $dump 'alignment_samples.json'),
        (Join-Path $dump 'fog_of_war.json'),
        (Join-Path $dump 'meta.json')
    )
    if (-not (Test-Path $maskPng) -or -not (Test-Path $maskJson)) {
        $skip += "$scene (no mask)"
        continue
    }
    if ($need | Where-Object { -not (Test-Path $_) }) {
        $skip += "$scene (incomplete dump)"
        continue
    }
    if (-not (Get-ChildItem -Path $dump -Filter 'terrain_*_meta.json' -File -ErrorAction SilentlyContinue)) {
        $skip += "$scene (no terrain tiles)"
        continue
    }

    $out = Join-Path $OutDir "map_bg_${scene}_new.png"
    Write-Host "=== $scene -> $out" -ForegroundColor Cyan
    & python $script $dump --size $Size --out $out
    if ($LASTEXITCODE -ne 0) {
        $fail += $scene
        Write-Host "FAILED $scene (exit $LASTEXITCODE)" -ForegroundColor Red
        continue
    }
    $ok += $scene
}

Write-Host ""
Write-Host "done: $($ok.Count) ok, $($skip.Count) skipped, $($fail.Count) failed" -ForegroundColor Green
if ($ok) { Write-Host ("ok: " + ($ok -join ', ')) }
if ($skip) { Write-Host ("skip: " + ($skip -join ', ')) -ForegroundColor Yellow }
if ($fail) { Write-Host ("fail: " + ($fail -join ', ')) -ForegroundColor Red; exit 1 }
