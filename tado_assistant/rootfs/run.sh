#!/usr/bin/with-contenv bashio
set -euo pipefail

# Log-Level aus Add-on Konfiguration
export LOG_LEVEL="$(bashio::config 'log_level' 2>/dev/null || echo info)"

# WICHTIG: Flask Debug/ReLoader MUSS aus sein, sonst beendet sich PID1 (Restart-Loop).
export FLASK_ENV="production"
export FLASK_DEBUG="0"

# Ingress-Port (optional über ENV überschreibbar)
export PORT="${PORT:-8099}"

echo "[tado-assistant] run.sh: LOG_LEVEL=${LOG_LEVEL} PORT=${PORT} FLASK_ENV=${FLASK_ENV} FLASK_DEBUG=${FLASK_DEBUG}"

# Worker im Hintergrund starten (MQTT / Polling / Updates)
python3 /worker.py &
WORKER_PID=$!

_term() {
  kill -TERM "$WORKER_PID" 2>/dev/null || true
  wait "$WORKER_PID" 2>/dev/null || true
}
trap _term TERM INT

# Web-UI (Ingress) im Vordergrund starten
exec python3 /app.py
