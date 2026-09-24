#!/bin/bash
set -u
cd "$(dirname "$0")"

echo "Fomo Early-Signal Bot v16.2 QUALITY MEASUREMENT launcher"

if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3 is missing. Run: xcode-select --install"
  read -p "Press Enter to close."
  exit 1
fi

if [ ! -d ".venv" ]; then
  echo "Creating local Python environment..."
  python3 -m venv .venv || exit 1
fi

PY="./.venv/bin/python3"
"$PY" -m pip install -q -r requirements.txt || {
  echo "Dependency install failed. Check internet and retry."
  read -p "Press Enter to close."
  exit 1
}

# Import ONLY API/Telegram secrets from the exact prior bot or older versions.
if [ ! -f ".env" ]; then
  "$PY" import_secrets.py
  import_code=$?
  if [ "$import_code" -eq 2 ]; then
    "$PY" setup_wizard.py
  elif [ "$import_code" -ne 0 ]; then
    echo "Secret import failed."
    read -p "Press Enter to close."
    exit 1
  fi
fi

# Persistent state lives outside version folders; migrate/recover it below.

# Add settings introduced in this build without replacing keys or existing custom values.
"$PY" migrate_env.py

# Recover existing positions/history into the shared state database.
"$PY" migrate_state.py

echo "Running v16.2 QUALITY MEASUREMENT self-check..."
"$PY" doctor.py
echo
echo "Starting v16.2 QUALITY MEASUREMENT. Mac sleep prevention ON. Crash auto-restart ON. Continuous position guardian ON."
echo "Telegram: /help /status /sources /edge /leaders /shadow /paperreport /bankroll /why /positions /hold /guardian /wallet /sync /learn /daily
Recommended first run: keep real autopilot OFF and leave /shadow on. Optional: set PUBLIC wallet sync, Jupiter API key, Helius API key, PumpPortal/X; see README and /sources."
echo

while true; do
  if command -v caffeinate >/dev/null 2>&1; then
    PYTHONUNBUFFERED=1 caffeinate -dims "$PY" -u bot.py 2>&1 | tee -a bot.log
  else
    PYTHONUNBUFFERED=1 "$PY" -u bot.py 2>&1 | tee -a bot.log
  fi
  code=${PIPESTATUS[0]}
  if [ "$code" -eq 0 ] || [ "$code" -eq 130 ]; then
    break
  fi
  echo "Bot exited with code $code. Restarting in 8 seconds..."
  sleep 8
done
