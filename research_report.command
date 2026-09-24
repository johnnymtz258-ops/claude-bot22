#!/bin/bash
# Read-only edge research over the shared bot database. Writes RESEARCH_REPORT_<time>.txt here.
cd "$(dirname "$0")"
PY="./.venv/bin/python3"
if [ ! -x "$PY" ]; then PY="python3"; fi
"$PY" research_report.py
read -p "Press Enter to close."
