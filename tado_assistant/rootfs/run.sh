#!/bin/sh
set -eu

echo "[INFO] Starting Tado Assistant (no Flask) MARKER 2025-12-26-NOFLASK"

export PYTHONUNBUFFERED=1
export PORT="${PORT:-8099}"

# Worker im Hintergrund (crash-resistent machen wir gleich)
python3 -u /worker.py &

# API/Ingress Server im Vordergrund (PID1)
exec python3 -u /server.py
