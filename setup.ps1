# Create the venv and install dependencies. Run once.
#   .\setup.ps1
# If PowerShell refuses to run it:
#   powershell -ExecutionPolicy Bypass -File .\setup.ps1
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) { $py = Get-Command py -ErrorAction SilentlyContinue }
if (-not $py) { throw "No python on PATH. Install Python 3.9+ and retry." }

& $py.Source -m venv .venv
$venv = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $venv)) {
    # Usually means `python` resolved to the Microsoft Store stub, which exits
    # quietly instead of creating anything. Say so rather than fail later.
    throw "venv was not created ($venv). If 'python' opens the Microsoft Store, install Python from python.org or use 'py -m venv .venv'."
}
& $venv -m pip install --quiet --upgrade pip

# pycaw only powers the meeting-app audio signal, and it is the one dependency
# that pulls in a compiled wheel (psutil). If it will not install, carry on
# without it: the mic signal is stdlib and the calendar needs only comtypes,
# so the light still works and only the listen-only-webinar case is lost.
try {
    & $venv -m pip install --quiet -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw "pip exited $LASTEXITCODE" }
} catch {
    Write-Warning "Full install failed ($_). Retrying without pycaw - the meeting-app audio signal will be off."
    & $venv -m pip install --quiet pyserial comtypes
    if ($LASTEXITCODE -ne 0) { throw "pip could not install even pyserial and comtypes" }
}

Write-Host "venv ready: $venv"
Write-Host ""
Write-Host "Next: .\.venv\Scripts\python.exe meeting_status.py --probe"
