#!/usr/bin/with-contenv bashio
set -e

# Log-Level aus Add-on Konfiguration
export LOG_LEVEL="$(bashio::config 'log_level' || echo info)"

# Ingress/App Port (muss zu ingress_port in config.yaml passen)
export PORT="${PORT:-8099}"

# MQTT Optionen -> als ENV an den Worker (keine Credentials im Repo)
export MQTT_ENABLED="$(bashio::config 'mqtt_enable' || echo false)"
export MQTT_HOST="$(bashio::config 'mqtt_host' || echo '')"
export MQTT_PORT="$(bashio::config 'mqtt_port' || echo 1883)"
export MQTT_USERNAME="$(bashio::config 'mqtt_username' || echo '')"
export MQTT_PASSWORD="$(bashio::config 'mqtt_password' || echo '')"

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
