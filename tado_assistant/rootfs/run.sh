#!/usr/bin/env bash
set -euo pipefail

mkdir -p /data/tado_assistant

python3 /worker.py &
exec python3 /app.py
