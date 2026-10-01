#!/bin/bash
cd "$(dirname "$0")"
echo "Making a small review copy of your FomoBot database..."
PY="./.venv/bin/python3"
[ -x "$PY" ] || PY="python3"
"$PY" export_for_review.py
read -p "Press Enter to close."
