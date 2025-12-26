#!/bin/sh
set -eu

echo "[INFO] Starting Tado Assistant (Ingress) MARKER 2025-12-26-C"

export FLASK_ENV=production
export FLASK_DEBUG=0
export WERKZEUG_DEBUG_PIN=off
export PORT="${PORT:-8099}"

# Production WSGI server (kein Werkzeug-Reloader möglich)
exec gunicorn -b 0.0.0.0:${PORT} "app:app" \
  --workers 1 --threads 8 \
  --access-logfile - --error-logfile -
