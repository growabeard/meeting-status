#!/bin/zsh
# Unload and remove the launchd agent. Leaves the checkout alone.
PLIST=~/Library/LaunchAgents/local.meetingstatus.plist
launchctl unload "$PLIST" 2>/dev/null || true
rm -f "$PLIST"
echo "removed local.meetingstatus"
