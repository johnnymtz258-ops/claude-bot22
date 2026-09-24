#!/bin/bash
set -u
cd "$(dirname "$0")"
PY="./.venv/bin/python3"
if [ ! -x "$PY" ]; then
  echo "Start the bot once first so the local Python environment is created."
  read -p "Press Enter to close."
  exit 1
fi
"$PY" setup_wallet_sync.py
