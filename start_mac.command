#!/bin/bash
set -u
cd "$(dirname "$0")"

echo "🐋 FomoBot Whale Copy"

if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3 is missing. Run: xcode-select --install"
  read -p "Press Enter to close."
  exit 1
fi

if [ ! -d ".venv" ]; then
  echo "Creating local Python environment (first run only)..."
  python3 -m venv .venv || exit 1
fi

PY="./.venv/bin/python3"
"$PY" -m pip install -q -r requirements.txt || {
  echo "Installing dependencies failed. Check your internet connection and try again."
  read -p "Press Enter to close."
  exit 1
}

# First run: bring over Telegram/Helius/wallet settings from the previous bot folder, or ask.
if [ ! -f ".env" ]; then
  "$PY" import_secrets.py
  if [ $? -eq 2 ]; then
    "$PY" setup_wizard.py
  fi
fi

# Add any new settings to .env without touching your existing values.
"$PY" migrate_env.py

"$PY" doctor.py
echo
echo "Dashboard: http://localhost:8787   ·   Telegram: /help"
echo "Keep this window open. Mac sleep is blocked while the bot runs. Control+C stops it."
echo

while true; do
  if [ -f bot.log ] && [ "$(wc -c < bot.log | tr -d ' ')" -gt 52428800 ]; then
    mv -f bot.log bot.log.1
  fi
  if command -v caffeinate >/dev/null 2>&1; then
    PYTHONUNBUFFERED=1 caffeinate -dims "$PY" -u bot.py 2>&1 | tee -a bot.log
  else
    PYTHONUNBUFFERED=1 "$PY" -u bot.py 2>&1 | tee -a bot.log
  fi
  code=${PIPESTATUS[0]}
  if [ "$code" -eq 0 ] || [ "$code" -eq 130 ]; then
    break
  fi
  echo "Bot stopped unexpectedly (code $code). Restarting in 8 seconds..."
  sleep 8
done
