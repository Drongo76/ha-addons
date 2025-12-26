#!/usr/bin/with-contenv bashio
set -euo pipefail

export FLASK_ENV="production"
export FLASK_DEBUG="0"
export WERKZEUG_DEBUG_PIN="off"

export LOG_LEVEL="$(bashio::config 'log_level' 2>/dev/null || echo info)"
export PORT="${PORT:-8099}"

export MQTT_ENABLED="$(bashio::config 'mqtt_enable' 2>/dev/null || echo false)"
export MQTT_HOST="$(bashio::config 'mqtt_host' 2>/dev/null || echo '')"
export MQTT_PORT="$(bashio::config 'mqtt_port' 2>/dev/null || echo 1883)"
export MQTT_USERNAME="$(bashio::config 'mqtt_username' 2>/dev/null || echo '')"
export MQTT_PASSWORD="$(bashio::config 'mqtt_password' 2>/dev/null || echo '')"

python3 /worker.py &
WORKER_PID=$!

_term() {
  kill -TERM "$WORKER_PID" 2>/dev/null || true
  wait "$WORKER_PID" 2>/dev/null || true
}
trap _term TERM INT

exec python3 /app.py
