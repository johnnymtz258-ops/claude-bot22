#!/bin/bash
# Start FomoBot by itself whenever you log in to this Mac (it opens its usual window).
# Undo any time with remove_autostart.command.
cd "$(dirname "$0")"
DIR="$(pwd)"
PLIST="$HOME/Library/LaunchAgents/com.fomobot.whalecopy.plist"
mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.fomobot.whalecopy</string>
  <key>ProgramArguments</key><array>
    <string>/usr/bin/open</string><string>-a</string><string>Terminal</string><string>$DIR/start_mac.command</string>
  </array>
  <key>RunAtLoad</key><true/>
</dict></plist>
PL
launchctl unload "$PLIST" >/dev/null 2>&1
if launchctl load -w "$PLIST"; then
  echo "✅ FomoBot will start by itself every time you log in (from: $DIR)."
  echo "   If you move this folder, run this again. To undo: remove_autostart.command"
else
  echo "⚠️ Couldn't register auto-start. You can still double-click start_mac.command."
fi
read -p "Press Enter to close."
