#!/usr/bin/with-contenv bashio
set -e

export FLASK_ENV=production
export FLASK_DEBUG=0
export WERKZEUG_DEBUG_PIN=off
export PORT="${PORT:-8099}"

exec python3 /app.py
