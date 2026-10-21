#!/bin/bash
# Moves FomoBot to your always-on server (free Oracle Cloud / Google Cloud VM — see SERVER_SETUP.txt):
# copies this folder with your .env, the first time also your whales + history, and installs it there as a service.
# Run it again from each new version's folder to update the server (your server's history is kept).
set -u
cd "$(dirname "$0")"
CONF="$HOME/.fomobot_server"          # remembers the server's address (no secrets in it)
KEY="$HOME/.ssh/fomobot_ed25519"      # your login key for the server: the private half never leaves this Mac
STATE="$HOME/Library/Application Support/FomoBot"
REMOTE_STATE=".local/share/FomoBot"

pause() { read -r -p "Press Enter to close."; }
echo "☁️  Send FomoBot to your server"

if [ ! -f "$KEY" ]; then
  mkdir -p "$HOME/.ssh" && chmod 700 "$HOME/.ssh"
  ssh-keygen -q -t ed25519 -N "" -C "fomobot" -f "$KEY" || { echo "Couldn't create an SSH key."; pause; exit 1; }
fi

SERVER="$(cat "$CONF" 2>/dev/null | tr -d '[:space:]')"
if [ -z "$SERVER" ]; then
  echo
  echo "1) When you create the server, paste this PUBLIC key where it asks for an SSH key:"
  echo
  cat "$KEY.pub"
  pbcopy < "$KEY.pub" 2>/dev/null && echo "   (already copied — just paste it)"
  echo
  echo "2) When the server shows RUNNING, copy its public IP address."
  read -r -p "   Type the login here — ubuntu@IP for Oracle Cloud, fomobot@IP for Google Cloud: " SERVER
  SERVER="$(echo "$SERVER" | tr -d '[:space:]')"
  [ -z "$SERVER" ] && { echo "No server given."; pause; exit 1; }
  echo "$SERVER" > "$CONF"
fi

SSH_OPTS=(-i "$KEY" -o StrictHostKeyChecking=accept-new -o ConnectTimeout=20 -o ServerAliveInterval=30)
echo "Connecting to $SERVER ..."
if ! ssh "${SSH_OPTS[@]}" "$SERVER" true; then
  echo "❌ Can't log in to $SERVER."
  echo "   Check the IP, that the server is RUNNING and that you pasted the key shown above when you created it."
  echo "   Wrong address? Run this again after deleting it:  rm ~/.fomobot_server"
  pause; exit 1
fi

# Only one copy may run: two would send every alert twice — and make real trades twice.
if pgrep -f "python3 -u bot.py" >/dev/null 2>&1; then
  read -r -p "FomoBot is running on this Mac. Stop it so only the server runs it? [Y/n] " a
  if [ "${a:-Y}" != "n" ] && [ "${a:-Y}" != "N" ]; then
    pkill -INT -f "python3 -u bot.py"; sleep 4
  fi
fi
PLIST="$HOME/Library/LaunchAgents/com.fomobot.whalecopy.plist"
if [ -f "$PLIST" ]; then
  launchctl unload "$PLIST" >/dev/null 2>&1; rm -f "$PLIST"
  echo "Turned off starting by itself on this Mac (the server runs it now)."
fi

echo "Copying the bot to the server (your .env goes over an encrypted connection)..."
ssh "${SSH_OPTS[@]}" "$SERVER" 'command -v rsync >/dev/null || (sudo apt-get install -y -qq rsync || sudo dnf install -y -q rsync) >/dev/null 2>&1'
if ! rsync -az --exclude .venv --exclude .venv-day --exclude __pycache__ --exclude 'bot.log*' --exclude 'daybot.log*' \
     --exclude '*.zip' --exclude .DS_Store \
     -e "ssh ${SSH_OPTS[*]}" ./ "$SERVER:fomobot/"; then
  echo "❌ Copying failed."; pause; exit 1
fi

# Your whales, paper/live history and settings: copied the first time (or if you ask), never silently overwritten.
if [ -f "$STATE/whales.db" ]; then
  HAS=$(ssh "${SSH_OPTS[@]}" "$SERVER" "test -f $REMOTE_STATE/whales.db && echo yes || echo no")
  COPY=y
  if [ "$HAS" = "yes" ]; then
    read -r -p "The server already has its own history. Replace it with this Mac's? Only if the Mac's is newer. [y/N] " COPY
  fi
  if [ "$COPY" = "y" ] || [ "$COPY" = "Y" ]; then
    PY="./.venv/bin/python3"; [ -x "$PY" ] || PY="python3"
    SNAP="$(mktemp -d)/whales.db"
    "$PY" -c "import sqlite3,sys; s=sqlite3.connect('file:'+sys.argv[1]+'?mode=ro', uri=True); d=sqlite3.connect(sys.argv[2]); s.backup(d); d.close(); s.close()" \
      "$STATE/whales.db" "$SNAP" || { echo "❌ Couldn't read this Mac's database."; pause; exit 1; }
    echo "Copying your whales and history ($(du -h "$SNAP" | cut -f1))..."
    ssh "${SSH_OPTS[@]}" "$SERVER" "sudo systemctl stop fomobot 2>/dev/null; mkdir -p $REMOTE_STATE && rm -f $REMOTE_STATE/whales.db-wal $REMOTE_STATE/whales.db-shm"
    scp -q "${SSH_OPTS[@]}" "$SNAP" "$SERVER:$REMOTE_STATE/whales.db" || { echo "❌ Copying the database failed."; pause; exit 1; }
    rm -f "$SNAP"
  fi
fi

TZ_NAME="$(readlink /etc/localtime 2>/dev/null | sed 's#.*zoneinfo/##')"
echo "Installing on the server..."
ssh -t "${SSH_OPTS[@]}" "$SERVER" "cd fomobot && FOMO_TZ='$TZ_NAME' bash server/install.sh"
# DayBot (stocks + options) too, once its market-data keys are in .env
if grep -q "^ALPACA_API_KEY_ID=." .env 2>/dev/null; then
  echo "Installing DayBot on the server..."
  ssh -t "${SSH_OPTS[@]}" "$SERVER" "cd fomobot && bash server/install_daybot.sh"
fi
echo
echo "Done. Telegram says where it's running. Your Mac can sleep now."
echo "Dashboard: double-click open_server_dashboard.command · Log: server_log.command"
pause
