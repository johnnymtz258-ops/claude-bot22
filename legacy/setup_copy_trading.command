#!/bin/bash
set -u
cd "$(dirname "$0")"
if [ ! -d ".venv" ]; then
  python3 -m venv .venv || exit 1
fi
PY="./.venv/bin/python3"
"$PY" setup_copy_trading.py
