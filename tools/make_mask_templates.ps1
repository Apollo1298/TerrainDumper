<#
.SYNOPSIS
Generate exclusion-mask paint templates from TerrainDumper dumps.

.DESCRIPTION
Runs tools/make_mask_template.py for each dump under DumpRoot (or for the
scene names you pass). Always overwrites masks/<Scene>/template.png and mask.json (bounds/size are
preserved from existing mask.json unless you pass --recompute-bounds). Does not
touch painted mask.png files.

.EXAMPLE
./tools/make_mask_templates.ps1

.EXAMPLE
./tools/make_mask_templates.ps1 LakeRegion CoastalRegion

.EXAMPLE
./tools/make_mask_templates.ps1 -MetersPerPixel 2

Requires TLD_PATH or TERRAIN_DUMPER_ROOT (or -DumpRoot).
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0, ValueFromRemainingArguments = $true)]
    [string[]]$Scenes,

    [string]$DumpRoot = '',

    [double]$MetersPerPixel = 2.0,

    [string]$OutDir
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '_ResolveTldPaths.ps1')
$DumpRoot = Get-TerrainDumperRoot -DumpRoot $DumpRoot

$repo = Split-Path -Parent $PSScriptRoot
$script = Join-Path $PSScriptRoot 'make_mask_template.py'
if (-not (Test-Path $script)) {
    throw "Missing $script"
}
if (-not (Test-Path $DumpRoot)) {
    throw "Dump root not found: $DumpRoot"
}

$masksRoot = if ($OutDir) { $OutDir } else { Join-Path $repo 'masks' }

if ($Scenes -and $Scenes.Count -gt 0) {
    $targets = @()
    foreach ($scene in $Scenes) {
        $dump = Join-Path $DumpRoot $scene
        if (-not (Test-Path $dump)) {
            Write-Warning "No dump for '$scene' at $dump — skip"
            continue
        }
        $targets += Get-Item $dump
    }
}
else {
    $targets = @(Get-ChildItem -Path $DumpRoot -Directory -ErrorAction Stop |
        Where-Object { Test-Path (Join-Path $_.FullName 'meta.json') })
}

if ($targets.Count -eq 0) {
    throw "No dumps to process under $DumpRoot"
}

$ok = 0
$fail = 0
Write-Host ("make_mask_templates: {0} dump(s) at {1} m/px -> {2}" -f $targets.Count, $MetersPerPixel, $masksRoot) -ForegroundColor Cyan

foreach ($dump in $targets) {
    $scene = $dump.Name
    Write-Host "--- $scene ---" -ForegroundColor Yellow
    $argList = @(
        $script,
        $dump.FullName,
        '--meters-per-pixel', $MetersPerPixel,
        '--out-dir', $masksRoot
    )
    & python @argList
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "make_mask_template failed for $scene (exit $LASTEXITCODE)"
        $fail++
        continue
    }
    $ok++
}

Write-Host "done: $ok ok, $fail failed" -ForegroundColor $(if ($fail -eq 0) { 'Green' } else { 'Yellow' })
if ($fail -gt 0) {
    exit 1
}
