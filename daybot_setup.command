#!/bin/bash
# Enter or change DayBot's keys (Alpaca, SnapTrade, its Telegram bot, optional Claude / Finnhub). Double-click it.
cd "$(dirname "$0")"
PYBIN=""
for v in 3.13 3.12 3.11 3.10 3.14 3.15; do
  if command -v "python$v" >/dev/null 2>&1; then PYBIN="python$v"; break; fi
done
[ -z "$PYBIN" ] && PYBIN="python3"
if [ ! -d ".venv-day" ]; then
  "$PYBIN" -m venv .venv-day || exit 1
fi
PY="./.venv-day/bin/python3"
"$PY" -m pip install -q -r requirements-day.txt >/dev/null 2>&1 || true
"$PY" import_secrets.py || true
"$PY" daybot_setup.py
echo
read -p "Done. Press Enter to close this window."
