#!/usr/bin/env bash
# Run the flight checker locally. Creates a venv + installs deps on first run,
# then runs the scanner. Loads TELEGRAM_* from .env automatically.
#
#   ./run.sh            → full scan, sends to Telegram (needs .env filled in)
#   ./run.sh --local    → full scan, prints to terminal (no Telegram needed)
#   ./run.sh --local --max-routes 6   → quick smoke test
#
set -euo pipefail
cd "$(dirname "$0")"

PY=".venv/bin/python"

if [ ! -x "$PY" ]; then
  echo "📦 First run — creating .venv and installing dependencies…"
  python3 -m venv .venv
  .venv/bin/pip install --quiet --upgrade pip
  .venv/bin/pip install --quiet -r requirements.txt
fi

exec "$PY" checker.py "$@"
