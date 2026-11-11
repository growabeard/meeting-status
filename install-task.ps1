# Register the scheduled task with this checkout's absolute paths and start it.
# The Windows twin of install-agent.sh.
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

$venv = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $venv)) { throw "run .\setup.ps1 first" }
if (-not (Test-Path (Join-Path $PSScriptRoot 'config.json'))) {
    Copy-Item config.example.json config.json
}

$logDir = Join-Path $env:LOCALAPPDATA 'meeting-status'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir 'meeting-status.log'

# A foreground run first, so a broken config or a missing dependency shows up
# here with its error on screen rather than silently in the task's log.
& $venv meeting_status.py --config "config.json" --once --no-serial
if ($LASTEXITCODE -ne 0) {
    Write-Warning "a --once run failed; check: $venv meeting_status.py --probe"
}

# One Replace per statement: PowerShell 5.1, which is what Windows ships,
# cannot parse a method chain broken across lines. String .Replace, not the
# -replace operator, because paths are literals and not regexes.
$xml = Get-Content -Raw 'meeting-status.task.xml.template'
$xml = $xml.Replace('__DIR__', $PSScriptRoot)
$xml = $xml.Replace('__LOG__', $log)
$xml = $xml.Replace('__USER__', "$env:USERDOMAIN\$env:USERNAME")

# schtasks /XML wants the encoding to match the declaration, and the template
# declares UTF-16. PowerShell's "Unicode" is UTF-16LE with a BOM.
$tmp = Join-Path $env:TEMP 'meeting-status.task.xml'
Set-Content -Path $tmp -Value $xml -Encoding Unicode

schtasks /Create /TN 'meeting-status' /XML $tmp /F
if ($LASTEXITCODE -ne 0) { throw "schtasks /Create failed ($LASTEXITCODE)" }
Remove-Item $tmp -ErrorAction SilentlyContinue

schtasks /Run /TN 'meeting-status' | Out-Null
Write-Host "registered and started: meeting-status"
Write-Host "log: Get-Content -Wait '$log'"
