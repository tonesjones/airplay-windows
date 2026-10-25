<#
.SYNOPSIS
    Writes a redacted diagnostics zip to diagnostics\.

.DESCRIPTION
    Runs `py -3 -m airmirror.diagnose`. Extra arguments pass through, for example:
    .\diagnose.ps1 --session 192.168.0.45 --port 34247 --seconds 90
    The session mode keeps the console attached so you can type the PIN shown on the TV.
#>
$ErrorActionPreference = 'Stop'

# A shell opened before Go or FFmpeg was installed has a stale PATH.
$env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' +
            [Environment]::GetEnvironmentVariable('Path', 'User')

Push-Location $PSScriptRoot
try {
    # Output is not captured, so the sender's PIN prompt stays visible in --session mode.
    & py -3 -m airmirror.diagnose @args
    if ($LASTEXITCODE -ne 0) { throw "The diagnostics module failed (exit code $LASTEXITCODE)." }
    Write-Host ''
    Write-Host 'Review the zip before sharing it: open it and check each file. Redaction is best effort.'
} finally {
    Pop-Location
}
