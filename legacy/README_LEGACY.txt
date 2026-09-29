This folder is the previous FomoBot (v16.2 scanner + optional live autopilot), kept exactly as it
was so nothing is lost. It no longer starts from start_mac.command in the main folder.

Its own two-month review (OVERNIGHT_REPORT.txt) found the scanner signals lost money after fees.
The bot was rebuilt around whale copy trading instead — see ../README.md.

If you ever need it: open a Terminal in this folder and run  python3 bot.py
(it uses its own database, fomo_master.db, which the new bot never changes).
