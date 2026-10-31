<#
.SYNOPSIS
    Adds AirMirror shortcuts to the desktop and the Start menu.

.DESCRIPTION
    The shortcuts run "pyw -3 -m airmirror" from this folder, so the window opens without a
    console. Rerun this script after moving the repo folder.
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'

$pyw = Get-Command pyw.exe -ErrorAction SilentlyContinue
if (-not $pyw) {
    throw 'pyw.exe is missing. Install Python from python.org with the "py launcher" option.'
}

$shell = New-Object -ComObject WScript.Shell
foreach ($folder in [Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('Programs')) {
    $path = Join-Path $folder 'AirMirror.lnk'
    $link = $shell.CreateShortcut($path)
    $link.TargetPath = $pyw.Source
    $link.Arguments = '-3 -m airmirror'
    $link.WorkingDirectory = $PSScriptRoot
    $link.IconLocation = Join-Path $PSScriptRoot 'airmirror\airmirror.ico'
    $link.Description = 'Mirror this PC to an AirPlay TV'
    $link.Save()
    Write-Host "Created $path"
}
