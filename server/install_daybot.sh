#!/bin/bash
# Installs DayBot (the stocks + options day-trading assistant) as an always-on service next to FomoBot.
# send_to_server.command runs this for you once ALPACA keys are in .env. By hand, from the bot folder:
#   bash server/install_daybot.sh
set -eu
cd "$(dirname "$0")/.."
DIR="$(pwd)"
ME="$(id -un)"

echo "📈 Installing DayBot in $DIR (as $ME)"
PYBIN=python3
for v in 3.13 3.12 3.11 3.10; do
  if command -v "python$v" >/dev/null 2>&1; then PYBIN="python$v"; break; fi
done
[ -x .venv-day/bin/python3 ] || "$PYBIN" -m venv .venv-day
PY="$DIR/.venv-day/bin/python3"
"$PY" -m pip install -q --upgrade pip >/dev/null
"$PY" -m pip install -q -r requirements-day.txt
[ -f .env ] || "$PY" daybot_setup.py
chmod 600 .env

sudo tee /etc/systemd/system/daybot.service >/dev/null <<UNIT
[Unit]
Description=DayBot day-trading assistant
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=0

[Service]
Type=simple
User=$ME
WorkingDirectory=$DIR
Environment=PYTHONUNBUFFERED=1
ExecStart=$PY -u day.py
Restart=always
RestartSec=10
KillSignal=SIGINT
TimeoutStopSec=20

[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable daybot >/dev/null 2>&1
"$PY" daybot_doctor.py || true
sudo systemctl restart daybot
sleep 4
if systemctl is-active --quiet daybot; then
  echo "✅ DayBot is running on this server — it starts on boot and restarts by itself."
else
  echo "⚠️ DayBot didn't stay up. Last lines of its log:"
  sudo journalctl -u daybot -n 30 --no-pager
fi
echo "   Live log:  journalctl -u daybot -f        Stop: sudo systemctl stop daybot"
