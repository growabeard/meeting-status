# Read and override the meeting light. The Windows twin of bin/ms.
# No daemon, no port: the override is just a file, and the daemon notices it
# on its next poll.
#
#   .\ms.ps1              what the light is doing and why
#   .\ms.ps1 meeting      force red   (busy / off / free / upcoming also work)
#   .\ms.ps1 auto         back to automatic detection
#   .\ms.ps1 log          follow the daemon log
param([string]$Action = 'status')

$override = Join-Path $HOME '.meeting-status'
$log = Join-Path $env:LOCALAPPDATA 'meeting-status\meeting-status.log'

switch ($Action.ToLower()) {
    'status' {
        if (Test-Path $override) {
            "override: $((Get-Content $override -Raw).Trim())  (ms auto to clear)"
        } else {
            "override: none"
        }
        # The daemon logs every state change, so the last state line is current.
        $line = $null
        if (Test-Path $log) {
            $line = Get-Content $log |
                Select-String -Pattern '^[\d-]+ [\d:]+  [A-Z]+  (<-|\.\.)' |
                Select-Object -Last 1
        }
        if ($line) {
            "light:    $(($line.Line -replace '^[\d:\- ]*', ''))"
        } else {
            "light:    unknown"
        }
        $task = schtasks /Query /TN 'meeting-status' 2>$null
        if (-not $task) { "task:     NOT REGISTERED" }
        elseif (-not ($task -match 'Running')) { "task:     NOT RUNNING" }
    }
    { $_ -in 'auto', 'clear', 'none' } {
        Remove-Item $override -ErrorAction SilentlyContinue
        "override cleared - back to automatic"
    }
    'log' {
        if (-not (Test-Path $log)) { throw "no log yet at $log" }
        Get-Content -Wait $log
    }
    { $_ -in 'meeting', 'busy', 'free', 'off', 'upcoming' } {
        $state = $Action.ToUpper()
        Set-Content -Path $override -Value $state -NoNewline
        "forced $state - ms auto to clear"
    }
    default {
        Write-Error "usage: ms [status|meeting|busy|free|off|upcoming|auto|log]"
        exit 2
    }
}
