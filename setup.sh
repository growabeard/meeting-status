#!/bin/zsh
# Create the venv and install pyserial. Run once.
set -e
cd "$(dirname "$0")"

PY=/opt/homebrew/bin/python3.13   # Homebrew Python if present, else whatever python3 is on PATH
[ -x "$PY" ] || PY=$(command -v python3)

"$PY" -m venv .venv
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt

echo "venv ready: $(pwd)/.venv/bin/python"
echo
echo "Next: ./.venv/bin/python meeting_status.py --probe"
