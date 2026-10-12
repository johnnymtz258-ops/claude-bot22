#!/bin/bash
# Shows what the bot on your server is printing, live (Control+C to stop watching — the bot keeps running).
KEY="$HOME/.ssh/fomobot_ed25519"
SERVER="$(cat "$HOME/.fomobot_server" 2>/dev/null | tr -d '[:space:]')"
if [ -z "$SERVER" ]; then
  echo "Run send_to_server.command first."; read -r -p "Press Enter to close."; exit 1
fi
ssh -t -i "$KEY" -o ServerAliveInterval=30 "$SERVER" "systemctl --no-pager status fomobot | head -4; journalctl -u fomobot -n 80 -f"
