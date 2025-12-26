#!/usr/bin/with-contenv bashio
set -euo pipefail

# Niemals Flask Debug/ReLoader in HA Add-on
unset FLASK_DEBUG
unset FLASK_ENV

export LOG_LEVEL="$(bashio::config 'log_level' 2>/dev/null || echo info)"
export PORT="${PORT:-8099}"

# Worker starten (optional)
python3 /worker.py &
WORKER_PID=$!

# Gunicorn starten (kein reloader, kein debug)
gunicorn --chdir / \
  --workers 1 \
  --threads 4 \
  --bind "0.0.0.0:${PORT}" \
  --access-logfile "-" \
  --error-logfile "-" \
  "app:app" &
WEB_PID=$!

_term() {
  kill -TERM "$WEB_PID" 2>/dev/null || true
  kill -TERM "$WORKER_PID" 2>/dev/null || true
  wait "$WEB_PID" 2>/dev/null || true
  wait "$WORKER_PID" 2>/dev/null || true
}
trap _term TERM INT

wait "$WEB_PID"
