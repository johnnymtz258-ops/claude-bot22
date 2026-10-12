#!/bin/bash
# Opens the dashboard of the bot running on your server, through an encrypted SSH tunnel (nothing is opened to the
# internet). Keep this window open while you use the dashboard; close it when you're done.
cd "$(dirname "$0")"
KEY="$HOME/.ssh/fomobot_ed25519"
SERVER="$(cat "$HOME/.fomobot_server" 2>/dev/null | tr -d '[:space:]')"
if [ -z "$SERVER" ]; then
  echo "Run send_to_server.command first."; read -r -p "Press Enter to close."; exit 1
fi
PORT=8787
if lsof -nP -iTCP:$PORT -sTCP:LISTEN >/dev/null 2>&1; then PORT=8788; fi   # the Mac's own bot is using 8787
echo "Server dashboard: http://localhost:$PORT  — keep this window open while you use it (Control+C closes it)."
( sleep 3; open "http://localhost:$PORT" ) &
ssh -i "$KEY" -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -L "$PORT:localhost:8787" "$SERVER"
echo "Tunnel closed."; read -r -p "Press Enter to close."
