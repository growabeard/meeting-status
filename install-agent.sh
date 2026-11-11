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

# Ask for calendar access here, in a terminal, before launchd ever starts it.
# Not because the agent cannot prompt - a launchd *user* agent runs in the GUI
# session and can - but because a TCC grant belongs to the identity that asked,
# so the agent has to ask again for itself anyway, and it can only afford to
# wait auth_wait_background seconds for an answer. Doing it here means the
# prompt arrives while you are sitting in front of it, and this run also
# surfaces a broken config with its error on screen.
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
