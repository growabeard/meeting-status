#!/bin/zsh
# Generate the launchd plist with this checkout's absolute paths and load it.
# launchd needs absolute paths and cannot expand ~, hence the template.
set -e
cd "$(dirname "$0")"
DIR=$(pwd)
LABEL=local.meetingstatus
PLIST=~/Library/LaunchAgents/$LABEL.plist

[ -x .venv/bin/python ] || { echo "run ./setup.sh first"; exit 1; }
[ -f config.json ] || cp config.example.json config.json

# Grant calendar access interactively before launchd ever starts it: a launchd
# agent cannot display a permission prompt, so it would silently never get one.
if ! ./.venv/bin/python meeting_status.py --once --no-serial >/dev/null 2>&1; then
  echo "warning: a --once run failed; check ./.venv/bin/python meeting_status.py --probe"
fi

mkdir -p ~/Library/LaunchAgents
sed -e "s|__DIR__|$DIR|g" -e "s|__HOME__|$HOME|g" \
    meeting-status.plist.template > "$PLIST"
plutil -lint "$PLIST"

launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo "loaded $LABEL"
echo "log: tail -f ~/Library/Logs/meeting-status.log"
