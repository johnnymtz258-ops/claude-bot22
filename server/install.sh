#!/bin/bash
# Installs FomoBot as an always-on service on a Linux server (Ubuntu / Debian; Oracle Linux and Fedora work too).
# send_to_server.command on your Mac runs this for you. By hand, from the bot folder:  bash server/install.sh
# It starts on boot and restarts by itself if it ever stops. Run it again after every update.
set -eu
cd "$(dirname "$0")/.."
DIR="$(pwd)"
ME="$(id -un)"

echo "🐋 Installing FomoBot in $DIR (as $ME)"

# 1. Python
if command -v apt-get >/dev/null 2>&1; then
  sudo apt-get update -qq
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3 python3-venv python3-pip sqlite3 rsync >/dev/null
elif command -v dnf >/dev/null 2>&1; then
  sudo dnf install -y -q python3 python3-pip sqlite rsync >/dev/null
fi

# 2. Small servers (1 GB of memory): add 1 GB of swap so installs and busy minutes don't get killed
MEM_MB=$(awk '/MemTotal/ {print int($2 / 1024)}' /proc/meminfo)
if [ "$MEM_MB" -lt 1500 ] && [ -z "$(swapon --show 2>/dev/null)" ]; then
  echo "Adding 1 GB of swap (this server has ${MEM_MB} MB of memory)..."
  sudo fallocate -l 1G /swapfile 2>/dev/null || sudo dd if=/dev/zero of=/swapfile bs=1M count=1024 status=none
  sudo chmod 600 /swapfile && sudo mkswap /swapfile >/dev/null && sudo swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
fi

# 3. The bot's own Python environment
[ -x .venv/bin/python3 ] || python3 -m venv .venv
PY="$DIR/.venv/bin/python3"
"$PY" -m pip install -q --upgrade pip >/dev/null
"$PY" -m pip install -q -r requirements.txt
"$PY" -m pip install -q -r requirements-live.txt || echo "(Live-trading support not installed — only needed if you turn on LIVE_TRADING.)"

# 4. Settings: .env comes from your Mac (send_to_server.command); without one, ask
if [ ! -f .env ]; then
  "$PY" setup_wizard.py
fi
"$PY" migrate_env.py
chmod 600 .env   # tokens and keys: readable by you only

# 5. Your time zone, so the daily summary and the daily loss limit use your day
if [ -n "${FOMO_TZ:-}" ] && command -v timedatectl >/dev/null 2>&1; then
  sudo timedatectl set-timezone "$FOMO_TZ" 2>/dev/null && echo "Time zone: $FOMO_TZ" || true
fi

# 6. The service: starts on boot, restarts 10 s after any stop
sudo tee /etc/systemd/system/fomobot.service >/dev/null <<UNIT
[Unit]
Description=FomoBot Whale Copy
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=0

[Service]
Type=simple
User=$ME
WorkingDirectory=$DIR
Environment=PYTHONUNBUFFERED=1
ExecStart=$PY -u bot.py
Restart=always
RestartSec=10
KillSignal=SIGINT
TimeoutStopSec=20

[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable fomobot >/dev/null 2>&1

"$PY" doctor.py || true
sudo systemctl restart fomobot
sleep 4
if systemctl is-active --quiet fomobot; then
  echo
  echo "✅ FomoBot is running on this server — it starts on boot and restarts by itself."
else
  echo
  echo "⚠️ The service didn't stay up. Last lines of its log:"
  sudo journalctl -u fomobot -n 30 --no-pager
fi
echo "   Live log:  journalctl -u fomobot -f        Stop: sudo systemctl stop fomobot     Start: sudo systemctl start fomobot"
