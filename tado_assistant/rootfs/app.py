#!/usr/bin/env python3
import json
import os
import time
from pathlib import Path
from flask import Flask, request, redirect, url_for, render_template_string

APP_DIR = Path("/data/tado_assistant")
APP_DIR.mkdir(parents=True, exist_ok=True)

AUTH_FILE = APP_DIR / "auth.json"

app = Flask(__name__)

HTML_INDEX = """
<!doctype html>
<html>
  <head>
    <meta charset="utf-8" />
    <title>Tado Assistant</title>
    <style>
      body { font-family: sans-serif; margin: 24px; max-width: 820px; }
      .card { border: 1px solid #3333; border-radius: 12px; padding: 16px; margin-bottom: 16px; }
      input { width: 100%; padding: 10px; border-radius: 10px; border: 1px solid #3333; }
      button { padding: 10px 14px; border-radius: 10px; border: none; cursor: pointer; }
      .btn { background: #ff4d2e; color: #fff; font-weight: 700; }
      .btn2 { background: #222; color: #fff; font-weight: 700; }
      .muted { color: #666; }
      .row { display: flex; gap: 12px; align-items: center; }
      .row > * { flex: 1; }
      code { background: #0001; padding: 3px 6px; border-radius: 6px; }
      a { word-break: break-all; }
    </style>
  </head>
  <body>
    <div class="card">
      <h2>Status</h2>
      <p><b>Auth:</b> {{ "Eingerichtet" if auth_ok else "Nicht eingerichtet" }} &nbsp; <b>Home ID:</b> {{ home_id or "—" }}</p>
      <p><b>Konto-Label:</b> {{ label or "—" }}</p>
    </div>

    <div class="card">
      <h2>Login (Device Code Flow)</h2>
      <p class="muted">Du startest hier den Login, bekommst einen Link + Code, bestätigst im Browser bei tado, dann holst du hier das Token ab.</p>
      <form method="post" action="auth/start">
        <div class="row">
          <input name="label" placeholder="Konto-Label (z.B. juergen@...)" value="{{ label or '' }}" />
          <button class="btn" type="submit">Login starten</button>
        </div>
      </form>
      <p style="margin-top:10px;">
        <a class="btn2" style="display:inline-block; padding:10px 14px; border-radius:10px; text-decoration:none;" href="{{ url_for('auth_page') }}">Login-Status öffnen</a>
      </p>
    </div>

    <div class="card">
      <h2>MQTT Entities in Home Assistant</h2>
      <p class="muted">Wenn MQTT im Add-on aktiviert ist, legt der Worker automatisch Entities an: pro tado Mobile Device ein <code>binary_sensor</code> (zuhause/weg) + ein Gesamt-Sensor (Anzahl zuhause).</p>
    </div>
  </body>
</html>
"""

HTML_AUTH = """
<!doctype html>
<html>
  <head>
    <meta charset="utf-8" />
    <title>Tado Login</title>
    <style>
      body { font-family: sans-serif; margin: 24px; max-width: 820px; }
      .card { border: 1px solid #3333; border-radius: 12px; padding: 16px; }
      button { padding: 10px 14px; border-radius: 10px; border: none; cursor: pointer; }
      .btn { background: #ff4d2e; color: #fff; font-weight: 700; }
      .btn2 { background: #222; color: #fff; font-weight: 700; }
      .muted { color: #666; }
      code { background: #0001; padding: 3px 6px; border-radius: 6px; }
      a { word-break: break-all; }
    </style>
  </head>
  <body>
    <div class="card">
      <h2>Login-Status</h2>
      {% if link and code %}
        <p><b>Restzeit:</b> {{ expires_in }} Sekunden</p>
        <p>1) Link öffnen und bei tado bestätigen:<br/>
          <a href="{{ link }}" target="_blank">{{ link }}</a>
        </p>
        <p>2) Falls nötig Code eingeben:<br/>
          <code>{{ code }}</code>
        </p>

        <form method="post" action="poll">
          <button class="btn" type="submit">Token abrufen</button>
          <a class="btn2" style="display:inline-block; padding:10px 14px; border-radius:10px; text-decoration:none;" href="{{ url_for('index') }}">Zurück</a>
        </form>

        <p class="muted" style="margin-top:10px;">Hinweis: Bitte nicht schneller als alle 5 Sekunden drücken.</p>
      {% else %}
        <p class="muted">Kein laufender Login. Geh zurück und drücke „Login starten“.</p>
        <a class="btn2" style="display:inline-block; padding:10px 14px; border-radius:10px; text-decoration:none;" href="{{ url_for('index') }}">Zurück</a>
      {% endif %}

      <hr/>
      <p><b>Aktueller Status:</b> Auth={{ "OK" if auth_ok else "NO" }} / Home ID={{ home_id or "—" }} / Konto-Label={{ label or "—" }}</p>
      {% if message %}
        <p><b>Info:</b> {{ message }}</p>
      {% endif %}
    </div>
  </body>
</html>
"""

def load_auth():
    if AUTH_FILE.exists():
        try:
            return json.loads(AUTH_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}

def save_auth(data: dict):
    AUTH_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")

def load_addon_options():
    try:
        with open("/data/options.json", "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

@app.get("/")
def index():
    auth = load_auth()
    return render_template_string(
        HTML_INDEX,
        auth_ok=bool(auth.get("access_token")),
        home_id=auth.get("home_id"),
        label=auth.get("label"),
    )

@app.get("/auth")
def auth_page():
    auth = load_auth()
    return render_template_string(
        HTML_AUTH,
        link=auth.get("device_link"),
        code=auth.get("user_code"),
        expires_in=auth.get("expires_in", 0),
        auth_ok=bool(auth.get("access_token")),
        home_id=auth.get("home_id"),
        label=auth.get("label"),
        message=auth.get("message"),
    )

@app.post("/auth/start")
def auth_start():
    # Wichtig: NICHT auf "../auth" redirecten (das macht /auth/auth)
    label = (request.form.get("label") or "").strip()
    auth = load_auth()
    auth["label"] = label
    auth["message"] = ""
    save_auth(auth)
    # Gehe 1 Ebene hoch => /auth
    return redirect("..")

@app.post("/auth/poll")
def auth_poll():
    # Der Worker macht das eigentliche Token-Polling und schreibt in auth.json.
    # Hier triggern wir nur „warte kurz“ UX.
    auth = load_auth()
    auth["message"] = "Token-Abfrage läuft… (bitte nach ein paar Sekunden Seite neu öffnen)"
    save_auth(auth)
    return redirect(url_for("auth_page"))

@app.post("/reset")
def reset():
    if AUTH_FILE.exists():
        AUTH_FILE.unlink()
    return redirect(url_for("index"))

if __name__ == "__main__":
    opts = load_addon_options()
    debug = bool(opts.get("debug", False))
    app.run(host="0.0.0.0", port=8099, debug=debug)
