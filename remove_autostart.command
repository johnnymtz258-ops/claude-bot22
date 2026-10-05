#!/bin/bash
# Stop FomoBot from starting by itself at login.
PLIST="$HOME/Library/LaunchAgents/com.fomobot.whalecopy.plist"
launchctl unload "$PLIST" >/dev/null 2>&1
rm -f "$PLIST"
echo "✅ Auto-start removed. Start the bot with start_mac.command when you want it."
read -p "Press Enter to close."
