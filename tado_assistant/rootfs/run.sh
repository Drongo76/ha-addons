#!/usr/bin/with-contenv bashio
set -euo pipefail

bashio::log.info "Starting Tado Assistant..."

# Log level
LOG_LEVEL="$(bashio::config 'log_level')"
export LOG_LEVEL="${LOG_LEVEL:-info}"

# MQTT options (NICHT hardcoded!)
export MQTT_ENABLED="$(bashio::config 'mqtt.enabled')"
export MQTT_HOST="$(bashio::config 'mqtt.host')"
export MQTT_PORT="$(bashio::config 'mqtt.port')"
export MQTT_USERNAME="$(bashio::config 'mqtt.username')"
export MQTT_PASSWORD="$(bashio::config 'mqtt.password')"
export MQTT_DISCOVERY_PREFIX="$(bashio::config 'mqtt.discovery_prefix')"
export MQTT_TOPIC_PREFIX="$(bashio::config 'mqtt.topic_prefix')"

# Ingress web port (muss zu config.yaml ingress_port passen)
export WEB_PORT="8099"

bashio::log.info "WEB_PORT=${WEB_PORT}, MQTT_ENABLED=${MQTT_ENABLED}"

# Worker im Hintergrund starten
python3 -u /worker.py &

# Webserver im Vordergrund (Container lebt)
exec python3 -u /app.py
