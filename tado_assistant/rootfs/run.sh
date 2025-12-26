#!/bin/bash
set -e

echo "[INFO] Starting Tado Assistant (Ingress) MARKER 2025-12-26-B"

export FLASK_ENV=production
export FLASK_DEBUG=0
export WERKZEUG_DEBUG_PIN=off
export PORT="${PORT:-8099}"

# Wichtig: WSGI Server (kein Reloader möglich)
exec gunicorn -b 0.0.0.0:${PORT} "app:app" \
  --workers 1 --threads 8 \
  --access-logfile - --error-logfile -
