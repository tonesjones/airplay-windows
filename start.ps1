<#
.SYNOPSIS
    Starts the AirPlay mirroring app.

.PARAMETER Browser
    Use the browser fallback (fallback\start-browser.ps1) instead of native AirPlay.

.PARAMETER Display
    Display index for the browser fallback.

.PARAMETER Port
    Port for the browser fallback.
#>
[CmdletBinding()]
param(
    [switch]$Browser,
    [int]$Display = 0,
    [int]$Port = 8080
)

$ErrorActionPreference = 'Stop'

# A shell opened before Go, FFmpeg or Python was installed has a stale PATH.
$env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' +
            [Environment]::GetEnvironmentVariable('Path', 'User')

Push-Location $PSScriptRoot
try {
    # setup.ps1 owns the build-stamp logic and is cheap when bin\ is up to date.
    & (Join-Path $PSScriptRoot 'setup.ps1')

    if ($Browser) {
        $fallback = Join-Path $PSScriptRoot 'fallback\start-browser.ps1'
        if (-not (Test-Path $fallback)) {
            Write-Host 'The browser fallback is not installed yet (fallback\start-browser.ps1 is missing).'
            Write-Host 'Run without -Browser to use native AirPlay.'
            return
        }
        & $fallback -Display $Display -Port $Port
    } else {
        & py -3 -m airmirror
        if ($LASTEXITCODE -ne 0) { throw "airmirror exited with code $LASTEXITCODE." }
    }
} finally {
    Pop-Location
}
