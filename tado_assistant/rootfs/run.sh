#!/usr/bin/with-contenv bashio
set -euo pipefail

# Marker: wenn du das NICHT im Log siehst, läuft nicht dein aktuelles Image!
echo "### TADO-ASSISTANT run.sh LOADED (v0.2.6) ###"

# Flask Debug/Reload MUSS aus (sonst: Restarting with stat + s6 loop)
export FLASK_ENV="production"
export FLASK_DEBUG="0"

export LOG_LEVEL="$(bashio::config 'log_level' 2>/dev/null || echo info)"
export PORT="${PORT:-8099}"

# Worker im Hintergrund (optional)
python3 /worker.py &
WORKER_PID=$!

_term() {
  kill -TERM "$WORKER_PID" 2>/dev/null || true
  wait "$WORKER_PID" 2>/dev/null || true
}
trap _term TERM INT

# UI im Vordergrund
exec python3 /app.py
