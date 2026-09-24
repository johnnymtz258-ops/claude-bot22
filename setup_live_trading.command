#!/bin/bash
set -u
cd "$(dirname "$0")"
if [ ! -d ".venv" ]; then
  python3 -m venv .venv || exit 1
fi
PY="./.venv/bin/python3"
"$PY" -m pip install -q -r requirements.txt || exit 1
"$PY" setup_live_trading.py
read -p "Press Enter to close."
