#!/usr/bin/env bash
set -e

mkdir -p /data

cat >/app.py <<'PY'
from flask import Flask
app = Flask(__name__)

@app.get("/")
def index():
    return """
    <h2>Tado Assistant</h2>
    <p>Weboberfläche ist aktiv (Ingress).</p>
    <p>Login/Setup kommt im nächsten Schritt.</p>
    """

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8099)
PY

exec python3 /app.py
