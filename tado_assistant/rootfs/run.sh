#!/usr/bin/env bash
set -e

mkdir -p /data

cat >/app.py <<'PY'
from flask import Flask

app = Flask(__name__, static_folder="/static", static_url_path="/static")

@app.get("/")
def index():
    return """
    <!doctype html>
    <html lang="de">
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>Tado Assistant</title>
      </head>
      <body style="font-family: Arial, Helvetica, sans-serif; margin: 24px;">
        <div style="max-width: 720px;">
          <img src="/static/tado.svg" alt="tado" style="height: 84px; display: block; margin-bottom: 18px;" />
          <h2 style="margin: 0 0 10px 0;">Tado Assistant</h2>
          <p style="margin: 0 0 18px 0;">Weboberfläche (Ingress) ist aktiv. Login/Benutzerdaten kommen als nächster Schritt.</p>
        </div>
      </body>
    </html>
    """

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8099)
PY

exec python3 /app.py
