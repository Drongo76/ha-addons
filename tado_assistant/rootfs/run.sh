#!/usr/bin/with-contenv bashio
set -e

# Log-Level aus Add-on Konfiguration
export LOG_LEVEL="$(bashio::config 'log_level')"

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
