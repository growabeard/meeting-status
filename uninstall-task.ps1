# Stop and remove the scheduled task. Leaves the checkout alone.
# The Windows twin of uninstall-agent.sh.
schtasks /End /TN 'meeting-status' 2>$null | Out-Null
schtasks /Delete /TN 'meeting-status' /F
Write-Host "removed meeting-status"
