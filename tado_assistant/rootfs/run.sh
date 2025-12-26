#!/usr/bin/with-contenv bashio
set -euo pipefail

# Alles killen, was Flask in Debug/ReLoader bringen könnte
unset FLASK_DEBUG
unset FLASK_ENV
unset FLASK_RUN_EXTRA_FILES
unset FLASK_RUN_FROM_CLI

export LOG_LEVEL="$(bashio::config 'log_level' 2>/dev/null || echo info)"
export PORT="${PORT:-8099}"

# Worker optional im Hintergrund
python3 /worker.py &
WORKER_PID=$!

_term() {
  kill -TERM "$WORKER_PID" 2>/dev/null || true
  wait "$WORKER_PID" 2>/dev/null || true
}
trap _term TERM INT

# WSGI-Server im Vordergrund (stabil unter s6!)
exec gunicorn \
  --workers 1 \
  --threads 4 \
  --bind "0.0.0.0:${PORT}" \
  --access-logfile "-" \
  --error-logfile "-" \
  "app:app"
