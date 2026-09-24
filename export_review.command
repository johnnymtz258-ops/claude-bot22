#!/bin/bash
# Compact performance-review export (small, split into upload-sized parts, keys scrubbed).
cd "$(dirname "$0")"
PY="./.venv/bin/python3"
if [ ! -x "$PY" ]; then PY="python3"; fi
"$PY" export_review.py
