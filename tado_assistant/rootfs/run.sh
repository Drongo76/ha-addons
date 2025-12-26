#!/usr/bin/with-contenv bashio
set -e

# Niemals Flask Debug/ReLoader in HA Add-on
unset FLASK_DEBUG
unset FLASK_ENV

export LOG_LEVEL="$(bashio::config 'log_level' 2>/dev/null || echo info)"
export PORT="${PORT:-8099}"

python3 /worker.py &
WORKER_PID=$!

_term() {
  kill -TERM "$WORKER_PID" 2>/dev/null || true
  wait "$WORKER_PID" 2>/dev/null || true
}
trap _term TERM INT

exec python3 /app.py
