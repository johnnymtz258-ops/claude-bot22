#!/bin/bash
cd "$(dirname "$0")"
PY="./.venv/bin/python3"
if [ ! -x "$PY" ]; then PY="python3"; fi
"$PY" export_data.py
