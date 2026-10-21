#!/bin/bash
# DayBot — the stocks + options day-trading assistant. Double-click to start. Control+C stops it.
set -u
cd "$(dirname "$0")"

echo "📈 DayBot"

# The newest Python 3.10+ on this Mac if there is one (Claude chart reading needs it); otherwise python3.
PYBIN=""
for v in 3.13 3.12 3.11 3.10; do
  if command -v "python$v" >/dev/null 2>&1; then PYBIN="python$v"; break; fi
done
[ -z "$PYBIN" ] && PYBIN="python3"
if ! command -v "$PYBIN" >/dev/null 2>&1; then
  echo "Python 3 is missing. Install it from python.org (3.12 or newer), then double-click this again."
  read -p "Press Enter to close."
  exit 1
fi

# Its own Python environment, separate from FomoBot's
if [ ! -d ".venv-day" ]; then
  echo "Creating DayBot's Python environment (first run only)..."
  "$PYBIN" -m venv .venv-day || exit 1
fi
PY="./.venv-day/bin/python3"
"$PY" -m pip install -q --upgrade pip >/dev/null 2>&1
"$PY" -m pip install -q -r requirements-day.txt || {
  echo "Installing failed. Check your internet connection and try again."
  read -p "Press Enter to close."
  exit 1
}

# Bring over settings you already entered in another bot folder (only what's missing here)
"$PY" import_secrets.py >/dev/null 2>&1 || true
# Ask for DayBot's keys while one it needs is still missing (Enter skips; double-click daybot_setup.command to change them)
for key in ALPACA_API_KEY_ID SNAPTRADE_CLIENT_ID DAY_TELEGRAM_BOT_TOKEN; do
  if ! grep -q "^$key=." .env 2>/dev/null; then
    "$PY" daybot_setup.py
    break
  fi
done

echo
echo "Dashboard: http://localhost:8788   ·   Telegram: /help   ·   link your broker: /connect"
echo "Keep this window open. Mac sleep is blocked while it runs. Control+C stops it."
echo

while true; do
  if [ -f daybot.log ] && [ "$(wc -c < daybot.log | tr -d ' ')" -gt 52428800 ]; then
    mv -f daybot.log daybot.log.1
  fi
  if command -v caffeinate >/dev/null 2>&1; then
    PYTHONUNBUFFERED=1 caffeinate -dims "$PY" -u day.py 2>&1 | tee -a daybot.log
  else
    PYTHONUNBUFFERED=1 "$PY" -u day.py 2>&1 | tee -a daybot.log
  fi
  code=${PIPESTATUS[0]}
  if [ "$code" -eq 0 ] || [ "$code" -eq 130 ]; then
    break
  fi
  echo "DayBot stopped unexpectedly (code $code). Restarting in 8 seconds..."
  sleep 8
done
