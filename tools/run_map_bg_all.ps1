<#
.SYNOPSIS
Run make_map_bg.py for every non-mod region under masks/.

.DESCRIPTION
Scene list = folders in masks/ minus Mod*. Each region still needs a painted
mask.png + mask.json and a complete TerrainDumper dump. Writes
map_bg_<Scene>_new.png under out\maps (or -OutDir).

Use -Jobs N to run up to N regions at once (default: min(4, CPU count)).

.EXAMPLE
./tools/run_map_bg_all.ps1

.EXAMPLE
./tools/run_map_bg_all.ps1 -Jobs 1

.EXAMPLE
./tools/run_map_bg_all.ps1 -Size 8192 -Jobs 2

.EXAMPLE
./tools/run_map_bg_all.ps1 RuralRegion TracksRegion

Requires TLD_PATH or TERRAIN_DUMPER_ROOT (or -DumpRoot).
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0, ValueFromRemainingArguments = $true)]
    [string[]]$Scenes,

    [string]$DumpRoot = '',

    [string]$OutDir,

    [int]$Size = 4096,

    # 0 = auto min(4, CPU count); otherwise 1..16 concurrent regions
    [int]$Jobs = 0
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '_ResolveTldPaths.ps1')
$DumpRoot = Get-TerrainDumperRoot -DumpRoot $DumpRoot

$repo = Split-Path -Parent $PSScriptRoot
$script = Join-Path $PSScriptRoot 'make_map_bg.py'
if (-not (Test-Path $script)) { throw "Missing $script" }
if (-not (Test-Path $DumpRoot)) { throw "Dump root not found: $DumpRoot" }

if ($Jobs -eq 0) {
    $cpu = [Environment]::ProcessorCount
    if ($cpu -lt 1) { $cpu = 1 }
    $Jobs = [Math]::Min(4, $cpu)
}
elseif ($Jobs -lt 1 -or $Jobs -gt 16) {
    throw "-Jobs must be 0 (auto) or 1..16 (got $Jobs)"
}

if (-not $OutDir) { $OutDir = Join-Path $repo 'out\maps' }
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$logDir = Join-Path $OutDir '_batch_logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

$maskRoot = Join-Path $repo 'masks'
if (-not (Test-Path $maskRoot)) { throw "Missing masks folder: $maskRoot" }

function Test-IsNonModScene([string]$name) {
    return -not ($name -like 'Mod*')
}

function Test-IsFinalMapScene([string]$name) {
    # Prison survival zone is dump/composite source only — paste into BlackrockRegion.
    return $name -ne 'BlackrockPrisonSurvivalZone'
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

$ok = [System.Collections.Generic.List[string]]::new()
$skip = [System.Collections.Generic.List[string]]::new()
$fail = [System.Collections.Generic.List[string]]::new()
$pending = [System.Collections.Generic.Queue[hashtable]]::new()

foreach ($scene in $scenes) {
    if (-not (Test-IsNonModScene $scene)) {
        $skip.Add("$scene (mod)") | Out-Null
        continue
    }
    if (-not (Test-IsFinalMapScene $scene)) {
        $skip.Add("$scene (composite source only — use BlackrockRegion)") | Out-Null
        continue
    }

    $dump = Join-Path $DumpRoot $scene
    if (-not (Test-Path $dump)) {
        $skip.Add("$scene (no dump)") | Out-Null
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
        $skip.Add("$scene (no mask)") | Out-Null
        continue
    }
    if ($need | Where-Object { -not (Test-Path $_) }) {
        $skip.Add("$scene (incomplete dump)") | Out-Null
        continue
    }
    if (-not (Get-ChildItem -Path $dump -Filter 'terrain_*_meta.json' -File -ErrorAction SilentlyContinue)) {
        $skip.Add("$scene (no terrain tiles)") | Out-Null
        continue
    }

    $out = Join-Path $OutDir "map_bg_${scene}_new.png"
    $pending.Enqueue(@{
        Scene = $scene
        Dump  = $dump
        Out   = $out
    }) | Out-Null
}

Write-Host "run_map_bg_all: $($pending.Count) queued, Jobs=$Jobs" -ForegroundColor Cyan

$running = [System.Collections.Generic.List[hashtable]]::new()

function Receive-FinishedJobs {
    for ($i = $running.Count - 1; $i -ge 0; $i--) {
        $job = $running[$i]
        $proc = $job.Process
        if (-not $proc.HasExited) { continue }

        $scene = $job.Scene
        $code = $proc.ExitCode
        Write-Host "=== $scene (exit $code) ===" -ForegroundColor $(if ($code -eq 0) { 'Green' } else { 'Red' })
        if (Test-Path $job.LogOut) { Get-Content -LiteralPath $job.LogOut }
        if (Test-Path $job.LogErr) {
            $errText = Get-Content -LiteralPath $job.LogErr -Raw
            if ($errText -and $errText.Trim().Length -gt 0) {
                Write-Host $errText -ForegroundColor Yellow
            }
        }
        if ($code -ne 0) {
            $fail.Add($scene) | Out-Null
            Write-Host "FAILED $scene (exit $code)" -ForegroundColor Red
        }
        else {
            $ok.Add($scene) | Out-Null
        }
        $running.RemoveAt($i)
    }
}

while ($pending.Count -gt 0 -or $running.Count -gt 0) {
    Receive-FinishedJobs
    while ($running.Count -lt $Jobs -and $pending.Count -gt 0) {
        $item = $pending.Dequeue()
        $scene = $item.Scene
        $logOut = Join-Path $logDir "${scene}.out.log"
        $logErr = Join-Path $logDir "${scene}.err.log"
        Remove-Item -LiteralPath $logOut, $logErr -ErrorAction SilentlyContinue

        $argList = @(
            $script,
            $item.Dump,
            '--size', "$Size",
            '--out', $item.Out
        )
        Write-Host ">>> start $scene -> $($item.Out)" -ForegroundColor Cyan
        $proc = Start-Process -FilePath 'python' -ArgumentList $argList `
            -WorkingDirectory $repo -NoNewWindow -PassThru `
            -RedirectStandardOutput $logOut -RedirectStandardError $logErr
        $running.Add(@{
            Scene   = $scene
            Process = $proc
            LogOut  = $logOut
            LogErr  = $logErr
        }) | Out-Null
    }
    if ($running.Count -gt 0) {
        Start-Sleep -Milliseconds 400
    }
}

Write-Host ""
Write-Host "done: $($ok.Count) ok, $($skip.Count) skipped, $($fail.Count) failed (Jobs=$Jobs)" -ForegroundColor Green
if ($ok.Count -gt 0) { Write-Host ("ok: " + ($ok -join ', ')) }
if ($skip.Count -gt 0) { Write-Host ("skip: " + ($skip -join ', ')) -ForegroundColor Yellow }
if ($fail.Count -gt 0) { Write-Host ("fail: " + ($fail -join ', ')) -ForegroundColor Red; exit 1 }
