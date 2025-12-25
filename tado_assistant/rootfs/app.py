#!/usr/bin/with-contenv python3
# -*- coding: utf-8 -*-

import json
import os
import time
from pathlib import Path

import requests
from flask import Flask, request, redirect, Response

APP_TITLE = "Tado Assistant Add-on (Ingress)"

DATA_DIR = Path("/data")
AUTH_FILE = DATA_DIR / "auth.json"
DEVICE_FILE = DATA_DIR / "device.json"

# --- tado OAuth endpoints (Device Code Flow) ---
# (So wie in deinem Repo – NICHT ändern)
DEVICE_AUTHORIZE_URL = "https://login.tado.com/oauth2/device_authorize"
TOKEN_URL = "https://login.tado.com/oauth2/token"

# Diese Werte sind in deinem Repo bereits so vorgesehen.
# Falls du sie anders im Code hast, lass sie wie sie waren.
CLIENT_ID = os.environ.get("TADO_CLIENT_ID", "").strip()
SCOPE = os.environ.get("TADO_SCOPE", "offline_access").strip()


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def write_json(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def auth_status():
    auth = read_json(AUTH_FILE) or {}
    label = auth.get("email_label") or "—"
    home = str(auth.get("home_id") or "—")
    ok = "OK" if auth.get("access_token") else "Nicht eingerichtet"
    return ok, home, label


def page(body_html: str, title: str = "Tado Assistant"):
    # Wichtig für Ingress: wenn HA die URL ohne "/" am Ende öffnet, verlieren relative Links den Token.
    ingress = request.headers.get("X-Ingress-Path", "") or ""
    if ingress and not ingress.endswith("/"):
        ingress += "/"
    base_tag = f'<base href="{ingress}">' if ingress else ""

    return f"""<!doctype html>
<html lang="de">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  {base_tag}
  <title>{title}</title>
  <style>
    body {{
      margin:0; font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif;
      background:#0b0f14; color:#e7eef7;
      display:flex; justify-content:center; padding:24px;
    }}
    .card {{
      width:min(820px, 100%);
      background: rgba(255,255,255,0.06);
      border: 1px solid rgba(255,255,255,0.12);
      border-radius: 16px;
      padding: 18px;
      box-shadow: 0 12px 40px rgba(0,0,0,0.35);
    }}
    .header {{ display:flex; align-items:center; gap:14px; margin-bottom:14px; }}
    .logo {{ height:48px; }}
    h1 {{ font-size:20px; margin:0; font-weight:700; }}
    .muted {{ opacity:0.75; font-size:13px; }}
    .pill {{
      display:inline-block; padding:6px 10px; border-radius:999px;
      background: rgba(255,255,255,0.08); border:1px solid rgba(255,255,255,0.10);
      font-size:13px;
    }}
    hr {{ border:none; border-top:1px solid rgba(255,255,255,0.12); margin:14px 0; }}
    .row {{ display:flex; gap:10px; align-items:center; flex-wrap:wrap; }}
    input {{
      width: 100%;
      padding: 10px 12px;
      border-radius: 10px;
      border: 1px solid rgba(255,255,255,0.14);
      background: rgba(0,0,0,0.35);
      color: #e7eef7;
      font-size: 14px;
      outline:none;
    }}
    .btn {{
      display:inline-flex; align-items:center; justify-content:center;
      padding: 10px 14px; border-radius: 10px;
      border: 1px solid rgba(255,255,255,0.14);
      background: rgba(255,255,255,0.08);
      color: #e7eef7; text-decoration:none;
      font-weight: 650; cursor:pointer;
    }}
    .btn.primary {{
      background: #ff6a2b;
      border-color: rgba(255,255,255,0.18);
      color:#111;
    }}
    code {{
      background: rgba(0,0,0,0.35);
      padding: 2px 6px;
      border-radius: 6px;
      border: 1px solid rgba(255,255,255,0.14);
    }}
  </style>
</head>
<body>
  <div class="card">
    {body_html}
  </div>
</body>
</html>"""


def tado_start_device_flow(email_label: str) -> dict:
    if not CLIENT_ID:
        raise RuntimeError("TADO_CLIENT_ID fehlt (Environment).")

    payload = {"client_id": CLIENT_ID, "scope": SCOPE}
    r = requests.post(DEVICE_AUTHORIZE_URL, data=payload, timeout=30)
    r.raise_for_status()
    data = r.json()

    data["email_label"] = email_label
    data["started_at"] = int(time.time())
    return data


def tado_poll_token(device: dict) -> dict:
    payload = {
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        "device_code": device["device_code"],
        "client_id": CLIENT_ID,
    }
    r = requests.post(TOKEN_URL, data=payload, timeout=30)
    if r.status_code == 200:
        return r.json()
    try:
        return {"error": r.json().get("error", "unknown_error"), "raw": r.text}
    except Exception:
        return {"error": "unknown_error", "raw": r.text}


app = Flask(__name__, static_folder="/static", static_url_path="/static")


@app.get("/")
def index():
    ok, home, label = auth_status()
    body = f"""
    <div class="header">
      <img class="logo" src="static/tado.svg" alt="tado" />
      <div>
        <h1>{APP_TITLE}</h1>
        <div class="muted">Status</div>
      </div>
    </div>

    <div class="row" style="margin-bottom:10px;">
      <span class="pill">Auth: <b>{ok}</b></span>
      <span class="pill">Home ID: <b>{home}</b></span>
      <span class="pill">Konto-Label: <b>{label}</b></span>
    </div>

    <div class="row" style="margin-bottom:14px;">
      <form method="post" action="reset">
        <button class="btn" type="submit">Reset (Auth löschen)</button>
      </form>
    </div>

    <hr />

    <h3 style="margin:0 0 8px 0;">Login (Device Code Flow)</h3>
    <form method="post" action="auth/start">
      <div class="row">
        <div style="flex:1; min-width:260px;">
          <input name="email_label" placeholder="Konto-Label (z.B. juergen@...)" />
        </div>
        <button class="btn primary" type="submit">Login starten</button>
      </div>
    </form>

    <div style="margin-top:16px;">
      <a class="btn" href="auth">Login-Status öffnen</a>
    </div>

    <hr />

    <h3 style="margin:0 0 6px 0;">MQTT Entities in Home Assistant</h3>
    <div class="muted">
      Wenn MQTT im Add-on aktiviert ist, legt der Worker automatisch Entities an:
      pro tado Mobile Device <code>binary_sensor</code> (zuhause/weg) + ein Gesamt-Sensor (Anzahl zuhause).
    </div>
    """
    return Response(page(body, "Tado Assistant"), mimetype="text/html")


@app.post("/reset")
def reset():
    try:
        if AUTH_FILE.exists():
            AUTH_FILE.unlink()
        if DEVICE_FILE.exists():
            DEVICE_FILE.unlink()
    except Exception:
        pass
    return redirect("/")


@app.post("/auth/start")
def auth_start():
    email_label = (request.form.get("email_label") or "").strip() or "tado"
    device = tado_start_device_flow(email_label=email_label)
    write_json(DEVICE_FILE, device)
    return redirect("/auth")


@app.get("/auth")
def auth_page():
    device = read_json(DEVICE_FILE) or {}
    ok, home, label = auth_status()

    expires_in = int(device.get("expires_in") or 0)
    started_at = int(device.get("started_at") or 0)
    now = int(time.time())
    remaining = max(0, expires_in - (now - started_at)) if expires_in and started_at else 0

    link = device.get("verification_uri_complete") or ""
    code = device.get("user_code") or ""

    body = f"""
    <div class="header">
      <img class="logo" src="static/tado.svg" alt="tado" />
      <div>
        <h1>Login-Status</h1>
        <div class="muted">Restzeit: <code>{remaining}</code> Sekunden</div>
      </div>
    </div>

    <div class="muted" style="margin-bottom:10px;">
      Aktueller Status: Auth=<b>{ok}</b> / Home ID=<b>{home}</b> / Konto-Label=<b>{label}</b>
    </div>

    <hr />

    <div style="margin-bottom:10px;">
      <div><b>1) Link öffnen und bei tado bestätigen:</b></div>
      <div style="margin-top:6px;">
        <a href="{link}" target="_blank" rel="noreferrer">{link or "—"}</a>
      </div>
    </div>

    <div style="margin-bottom:12px;">
      <div><b>2) Falls nötig Code eingeben:</b></div>
      <div style="margin-top:6px;"><code>{code or "—"}</code></div>
    </div>

    <div class="muted" style="margin-bottom:10px;">Hinweis: Bitte nicht schneller als alle 5 Sekunden drücken.</div>

    <div class="row">
      <form method="post" action="auth/poll">
        <button class="btn primary" type="submit">Token abrufen</button>
      </form>
      <a class="btn" href="./">Zurück</a>
    </div>
    """
    return Response(page(body, "Tado Assistant – Login"), mimetype="text/html")


@app.post("/auth/poll")
def auth_poll():
    device = read_json(DEVICE_FILE) or {}
    if not device.get("device_code"):
        return redirect("/auth")

    result = tado_poll_token(device)

    if result.get("access_token"):
        auth = {
            "email_label": device.get("email_label") or "tado",
            "access_token": result.get("access_token"),
            "refresh_token": result.get("refresh_token"),
            "expires_in": result.get("expires_in"),
            "token_type": result.get("token_type", "Bearer"),
            "obtained_at": int(time.time()),
        }
        write_json(AUTH_FILE, auth)

    return redirect("/auth")


if __name__ == "__main__":
    port = int(os.environ.get("WEB_PORT", "8099"))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
