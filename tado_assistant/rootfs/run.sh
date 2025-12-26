#!/bin/sh
set -eu

echo "[INFO] START MARKER PID1-FIX"

# Starte DEINEN Server (egal ob Flask oder http.server)
exec python3 -u /app.py
