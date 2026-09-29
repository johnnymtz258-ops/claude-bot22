#!/bin/bash
cd "$(dirname "$0")"
if [ ! -d ".venv" ]; then
  python3 -m venv .venv || exit 1
fi
./.venv/bin/python3 setup_edge_sources.py
