<#
.SYNOPSIS
    Shows the PC desktop in the TV's web browser (fallback when AirPlay does not work).

.DESCRIPTION
    Starts the MJPEG server and prints the address to type into the TV browser. Runs until Ctrl+C.
#>
[CmdletBinding()]
param(
    [int]$Display = 0,
    [int]$Port = 8080,
    [int]$Fps = 15,
    [int]$Width = 1920
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot

$env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' +
    [Environment]::GetEnvironmentVariable('Path', 'User')

if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    throw 'FFmpeg not found. Install it with: winget install Gyan.FFmpeg  (then open a new terminal)'
}

# LAN address of the interface that owns the default route.
$route = Get-NetRoute -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue |
    Sort-Object { $_.RouteMetric + $_.InterfaceMetric } | Select-Object -First 1
$ip = $null
if ($route) {
    $ip = Get-NetIPAddress -InterfaceIndex $route.InterfaceIndex -AddressFamily IPv4 `
        -ErrorAction SilentlyContinue | Where-Object { $_.IPAddress -notlike '169.254.*' } |
        Select-Object -First 1 -ExpandProperty IPAddress
}

if (Get-Command py -ErrorAction SilentlyContinue) { $python = @('py', '-3') } else { $python = @('python') }

Write-Host ''
Write-Host 'The first run shows a Windows Firewall prompt for python.exe.'
Write-Host 'Tick "Private networks" and click Allow, or the TV cannot connect.'
Write-Host ''

$serverArgs = @('-u', '-m', 'fallback.server', '--display', $Display, '--port', $Port,
    '--fps', $Fps, '--width', $Width)
if ($ip) { $serverArgs += @('--public-host', $ip) }

Push-Location $Root
# Windows PowerShell 5.1 turns native stderr into terminating errors under 'Stop'.
$ErrorActionPreference = 'Continue'
try {
    $exe = $python[0]
    $pre = @($python | Select-Object -Skip 1)
    & $exe @pre @serverArgs 2>&1 | ForEach-Object {
        $line = "$_"
        if ($line -match 'URL:\s*(http\S+)') {
            Write-Host ''
            Write-Host '  On the TV browser, open:' -ForegroundColor Yellow
            Write-Host ''
            Write-Host "      $($Matches[1])" -ForegroundColor Black -BackgroundColor Yellow
            Write-Host ''
        } else {
            Write-Host $line
        }
    }
} finally {
    Pop-Location
}
