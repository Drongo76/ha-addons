#!/usr/bin/with-contenv bashio
set -euo pipefail

python3 /worker.py &

exec python3 /app.py
