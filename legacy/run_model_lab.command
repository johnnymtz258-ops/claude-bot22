#!/bin/bash
cd "$(dirname "$0")"
PYTHON="python3"
if [ -x ".venv/bin/python" ]; then PYTHON=".venv/bin/python"; fi
"$PYTHON" model_lab.py
printf '\nPress Enter to close...'
read -r _
