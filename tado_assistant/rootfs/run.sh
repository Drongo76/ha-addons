#!/bin/sh
set -eu

echo "[INFO] NO-FLASK start MARKER 2025-12-26"

export PYTHONUNBUFFERED=1
export PORT="${PORT:-8099}"

exec python3 -u /app.py
