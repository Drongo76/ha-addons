#!/bin/sh
set -eu

export PORT="${PORT:-8099}"
export FLASK_ENV=production
export FLASK_DEBUG=0
export WERKZEUG_DEBUG_PIN=off

exec gunicorn -b 0.0.0.0:${PORT} "app:app" \
  --workers 1 --threads 8 \
  --access-logfile - --error-logfile -
