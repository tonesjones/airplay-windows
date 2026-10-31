<#
.SYNOPSIS
    One-time setup: installs Go and FFmpeg (pinned), then builds the AirPlay sender into bin\.

.DESCRIPTION
    Clones parsakhaz/open-airplay at a pinned commit into third_party\open-airplay, applies
    patches\*.patch in name order, and builds bin\open-airplay.exe plus
    bin\doubletake-test-receiver.exe (a local receiver used for simulated tests).
    Rerunning is cheap: it rebuilds only when the pinned commit or the patches change.

.PARAMETER Dev
    Also create .venv with the pinned test tools (pytest, ruff).

.PARAMETER Rebuild
    Rebuild even when bin\ is up to date.
#>
[CmdletBinding()]
param(
    [switch]$Dev,
    [switch]$Rebuild
)

$ErrorActionPreference = 'Stop'

$OpenAirPlayRepo = 'https://github.com/parsakhaz/open-airplay'
$OpenAirPlayCommit = '1e879a982df78b630a005fcee3ce278a56b3181d'
$GoVersion = '1.27.0'
$FfmpegVersion = '9.0.2'
$DevTools = @('pytest==9.1.1', 'ruff==0.17.2')

$Root = $PSScriptRoot
$Source = Join-Path $Root 'third_party\open-airplay'
$Bin = Join-Path $Root 'bin'
$Stamp = Join-Path $Bin 'build-stamp.txt'

function Invoke-Native {
    param([string]$What, [scriptblock]$Command)
    & $Command
    if ($LASTEXITCODE -ne 0) { throw "$What failed (exit code $LASTEXITCODE)." }
}

function Update-SessionPath {
    $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' +
                [Environment]::GetEnvironmentVariable('Path', 'User')
}

function Install-Tool {
    param([string]$Command, [string]$WingetId, [string]$Version)
    if (Get-Command $Command -ErrorAction SilentlyContinue) { return }
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        throw "$Command is missing and winget isn't available. Install $WingetId $Version manually, then rerun."
    }
    Write-Host "Installing $WingetId $Version with winget (Windows may ask for admin approval)..."
    Invoke-Native "winget install $WingetId" {
        winget install --id $WingetId --version $Version --exact --silent `
            --accept-package-agreements --accept-source-agreements
    }
    Update-SessionPath
    if (-not (Get-Command $Command -ErrorAction SilentlyContinue)) {
        throw "$Command is still not on PATH after installing. Open a new PowerShell window and rerun setup."
    }
}

function Get-Patches {
    Get-ChildItem (Join-Path $Root 'patches') -Filter '*.patch' -ErrorAction SilentlyContinue | Sort-Object Name
}

function Get-BuildStamp {
    $patches = Get-Patches | ForEach-Object { "$($_.Name) $((Get-FileHash $_.FullName -Algorithm SHA256).Hash)" }
    (@($OpenAirPlayCommit) + $patches) -join "`n"
}

# A shell opened before Go or FFmpeg was installed has a stale PATH.
Update-SessionPath
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    throw 'git is required. Install it with: winget install --id Git.Git --exact'
}
Install-Tool -Command 'go' -WingetId 'GoLang.Go' -Version $GoVersion
Install-Tool -Command 'ffmpeg' -WingetId 'Gyan.FFmpeg' -Version $FfmpegVersion

$ddagrab = (& ffmpeg -hide_banner -h filter=ddagrab 2>&1) -join "`n"
if ($ddagrab -notmatch 'Desktop Duplication') {
    throw "The ffmpeg on PATH ($((Get-Command ffmpeg).Source)) has no ddagrab filter. Install Gyan.FFmpeg $FfmpegVersion."
}

$wantStamp = (Get-BuildStamp).Trim()
$haveStamp = if (Test-Path $Stamp) { (Get-Content $Stamp -Raw).Trim() } else { '' }
$exe = Join-Path $Bin 'open-airplay.exe'

if ($Rebuild -or -not (Test-Path $exe) -or $haveStamp -ne $wantStamp) {
    if (-not (Test-Path (Join-Path $Source '.git'))) {
        Write-Host "Cloning open-airplay into $Source..."
        Invoke-Native 'git clone' { git clone --quiet $OpenAirPlayRepo $Source }
    }
    & git -C $Source rev-parse --verify --quiet "$OpenAirPlayCommit^{commit}" | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Invoke-Native 'git fetch' { git -C $Source fetch --quiet origin }
    }
    # third_party\open-airplay is managed by this script: reset it to the pin, then patch.
    Invoke-Native 'git reset' { git -C $Source reset --quiet --hard $OpenAirPlayCommit }
    Invoke-Native 'git clean' { git -C $Source clean -fdq }
    foreach ($patch in Get-Patches) {
        Write-Host "Applying $($patch.Name)..."
        Invoke-Native "git apply $($patch.Name)" { git -C $Source apply --whitespace=nowarn $patch.FullName }
    }

    New-Item -ItemType Directory -Force $Bin | Out-Null
    Write-Host 'Building the AirPlay sender (the first build downloads Go modules)...'
    Push-Location $Source
    try {
        Invoke-Native 'go build open-airplay' { go build -trimpath -o $exe ./cmd/open-airplay }
        Invoke-Native 'go build test receiver' {
            go build -trimpath -o (Join-Path $Bin 'doubletake-test-receiver.exe') ./cmd/doubletake-test-receiver
        }
    } finally {
        Pop-Location
    }
    Set-Content -Path $Stamp -Value $wantStamp -Encoding utf8
} else {
    Write-Host 'bin\open-airplay.exe is up to date.'
}

if ($Dev) {
    $venvPython = Join-Path $Root '.venv\Scripts\python.exe'
    if (-not (Test-Path $venvPython)) {
        Invoke-Native 'create .venv' { py -3 -m venv (Join-Path $Root '.venv') }
    }
    Invoke-Native 'install dev tools' { & $venvPython -m pip install --quiet --disable-pip-version-check @DevTools }
    Write-Host 'Dev tools ready: .venv\Scripts\python -m pytest ; .venv\Scripts\ruff check .'
}

& (Join-Path $Root 'shortcut.ps1')

Write-Host ''
Write-Host 'Setup complete. Open AirMirror from its desktop or Start menu shortcut.'
Write-Host 'The first time you mirror, Windows Firewall may ask about open-airplay.exe.'
Write-Host 'Allow it on Private networks so the TV can reach this PC.'
