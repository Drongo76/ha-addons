#!/usr/bin/with-contenv bashio
set -e

bashio::log.info "Starting Tado Assistant (Ingress) - repo build v0.3.0 (MARKER: 2025-12-26-A)"

export FLASK_ENV=production
export FLASK_DEBUG=0
export WERKZEUG_DEBUG_PIN=off
export PORT="${PORT:-8099}"

exec python3 -u /app.py
