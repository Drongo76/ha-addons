import os
import json
import time
import logging
from pathlib import Path
from flask import Flask, request, jsonify, Response

import requests

DATA_DIR = Path("/data")
DATA_DIR.mkdir(parents=True, exist_ok=True)

AUTH_FILE = DATA_DIR / "tado_auth.json"
DEVICE_FLOW_FILE = DATA_DIR / "device_flow.json"

# Tado OAuth (Device Code Flow)
DEVICE_AUTHORIZE_URL = "https://login.tado.com/oauth2/device_authorize"
TOKEN_URL = "https://login.tado.com/oauth2/token"

# Public client used by community implementations (device flow)
CLIENT_ID = "1bb50063-6b0c-4d11-bd99-387f4a91cc46"
SCOPE = "offline_access"

LOG_LEVEL = os.getenv("LOG_LEVEL", "info").lower()
logging.basicConfig(level=getattr(logging, LOG_LEVEL.upper(), logging.INFO))
log = logging.getLogger("tado_assistant_app")

app = Flask(__name__, static_folder="/static", static_url_path="/static")


def ingress_base() -> str:
    """
    Ingress adds X-Ingress-Path, e.g. /api/hassio_ingress/<token>
    We must build URLs relative to that base to avoid 404.
    """
    base = request.headers.get("X-Ingress-Path", "")
    if not base:
        return "/"
    if not base.endswith("/"):
        base += "/"
    return base


def load_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path: Path, data):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def html_page(body: str) -> Response:
    base = ingress_base()
    # IMPORTANT: <base href="..."> fixes all links/buttons under Ingress (no leading slash issues)
    page = f"""<!doctype html>
<html lang="de">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <base href="{base}">
  <title>Tado Assistant</title>
  <style>
    body {{ font-family: Arial, sans-serif; margin: 0; background:#0b0f14; color:#e8eef6; }}
    .wrap {{ max-width: 720px; margin: 0 auto; padding: 24px; }}
    .card {{ background:#111826; border:1px solid #263246; border-radius:16px; padding:18px; margin:16px 0; }}
    .row {{ display:flex; gap:12px; align-items:center; flex-wrap:wrap; }}
    .btn {{ background:#ff5a3d; color:#fff; border:0; padding:12px 16px; border-radius:12px; font-weight:700; cursor:pointer; }}
    .btn2 {{ background:#1c2a3f; color:#fff; border:1px solid #2b3a52; padding:12px 16px; border-radius:12px; font-weight:700; cursor:pointer; }}
    input {{ width: 100%; max-width:420px; padding:12px 12px; border-radius:12px; border:1px solid #2b3a52; background:#0b1220; color:#e8eef6; }}
    a {{ color:#7bc0ff; }}
    .muted {{ color:#9bb0c8; }}
    .logo {{ width:120px; height:auto; }}
    code {{ background:#0b1220; padding:2px 6px; border-radius:8px; border:1px solid #2b3a52; }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="row">
      <img class="logo" src="static/tado.svg" alt="tado"/>
      <div>
        <div class="muted">Tado Assistant Add-on (Ingress)</div>
      </div>
    </div>
    {body}
  </div>
</body>
</html>"""
    return Response(page, mimetype="text/html")


@app.get("/")
def index():
    auth = load_json(AUTH_FILE, default=None)
    email = ""
    if auth and isinstance(auth, dict):
        email = auth.get("account_label", "") or ""

    status_line = "Auth: <b>OK</b>" if auth and auth.get("refresh_token") else "Auth: <b>Nicht eingerichtet</b>"
    home_id = auth.get("home_id") if auth else None
    home_line = f"Home ID: <b>{home_id}</b>" if home_id else "Home ID: <b>—</b>"

    body = f"""
    <div class="card">
      <h2>Status</h2>
      <div>{status_line} &nbsp;&nbsp; {home_line}</div>
      <div class="muted" style="margin-top:8px;">Konto-Label: {email or "—"}</div>
    </div>

    <div class="card">
      <h2>Login (Device Code Flow)</h2>
      <div class="muted">
        Du startest hier den Login, bekommst einen Link + Code, bestätigst im Browser bei tado,
        dann holst du hier das Token ab.
      </div>
      <div style="margin-top:12px;">
        <input id="account_label" placeholder="Konto-Label (z.B. juergen@...)" value="{email}"/>
      </div>
      <div class="row" style="margin-top:12px;">
        <button class="btn" onclick="startLogin()">Login starten</button>
        <button class="btn2" onclick="location.href='login_status'">Login-Status öffnen</button>
      </div>
      <div id="msg" class="muted" style="margin-top:12px;"></div>
    </div>

    <div class="card">
      <h2>MQTT Entities in Home Assistant</h2>
      <div class="muted">
        Wenn MQTT im Add-on aktiviert ist, legt der Worker automatisch Entities an:
        pro Mobile Device ein <code>device_tracker</code> (home/not_home) + ein Sensor (Anzahl zuhause).
      </div>
    </div>

<script>
async function startLogin() {{
  const label = document.getElementById('account_label').value.trim();
  const res = await fetch('api/login/start', {{
    method: 'POST',
    headers: {{ 'Content-Type':'application/json' }},
    body: JSON.stringify({{ account_label: label }})
  }});
  const data = await res.json();
  const el = document.getElementById('msg');
  if (!res.ok) {{
    el.innerText = 'Fehler: ' + (data.error || 'unknown');
    return;
  }}
  el.innerHTML =
    'Link öffnen und bei tado bestätigen: <br><a href="' + data.verification_uri_complete + '" target="_blank">' +
    data.verification_uri_complete + '</a><br>' +
    'Code: <b>' + data.user_code + '</b><br>' +
    'Danach: <b>Login-Status öffnen</b> → <b>Token abrufen</b>.';
}}
</script>
    """
    return html_page(body)


@app.get("/login_status")
def login_status():
    flow = load_json(DEVICE_FLOW_FILE, default=None) or {}
    base = ingress_base()

    if not flow.get("device_code"):
        body = """
        <div class="card">
          <h2>Login-Status</h2>
          <div class="muted">Noch kein Login gestartet. Geh zurück und drücke <b>Login starten</b>.</div>
          <div class="row" style="margin-top:12px;">
            <button class="btn2" onclick="location.href='./'">Zurück</button>
          </div>
        </div>
        """
        return html_page(body)

    expires_at = flow.get("expires_at", 0)
    remaining = max(0, int(expires_at - time.time()))
    link = flow.get("verification_uri_complete", "")
    code = flow.get("user_code", "")

    body = f"""
    <div class="card">
      <h2>Login-Status</h2>
      <div class="muted">Restzeit: <b>{remaining}</b> Sekunden</div>

      <div style="margin-top:12px;">
        <div><b>1)</b> Link öffnen und bei tado bestätigen:</div>
        <div><a href="{link}" target="_blank">{link}</a></div>
      </div>

      <div style="margin-top:12px;">
        <div><b>2)</b> Falls nötig Code eingeben:</div>
        <div style="font-size:20px;"><b>{code}</b></div>
      </div>

      <div class="row" style="margin-top:16px;">
        <button class="btn" onclick="pollToken()">Token abrufen</button>
        <button class="btn2" onclick="location.href='./'">Zurück</button>
      </div>

      <div id="msg" class="muted" style="margin-top:12px;"></div>
      <div class="muted" style="margin-top:12px;">Hinweis: Bitte nicht schneller als alle 5 Sekunden drücken.</div>
    </div>

<script>
async function pollToken() {{
  const res = await fetch('api/login/poll', {{ method:'POST' }});
  const data = await res.json();
  const el = document.getElementById('msg');
  if (!res.ok) {{
    el.innerText = 'Status: ' + (data.error || 'unknown');
    return;
  }}
  el.innerHTML = '<b>OK!</b> Auth gespeichert. Du kannst zurück gehen.';
}}
</script>
    """
    return html_page(body)


@app.post("/api/login/start")
def api_login_start():
    payload = request.get_json(silent=True) or {}
    account_label = (payload.get("account_label") or "").strip()

    # Start device authorization
    r = requests.post(
        DEVICE_AUTHORIZE_URL,
        data={"client_id": CLIENT_ID, "scope": SCOPE},
        timeout=20,
    )
    if r.status_code >= 400:
        return jsonify({"error": f"device_authorize_failed_{r.status_code}", "details": r.text}), 400

    data = r.json()
    # Expect: device_code, user_code, verification_uri, verification_uri_complete, expires_in, interval
    expires_in = int(data.get("expires_in", 0) or 0)
    flow = {
        "account_label": account_label,
        "device_code": data.get("device_code"),
        "user_code": data.get("user_code"),
        "verification_uri": data.get("verification_uri"),
        "verification_uri_complete": data.get("verification_uri_complete") or data.get("verification_uri"),
        "interval": int(data.get("interval", 5) or 5),
        "expires_at": time.time() + expires_in,
    }
    save_json(DEVICE_FLOW_FILE, flow)

    return jsonify(
        {
            "account_label": account_label,
            "user_code": flow["user_code"],
            "verification_uri_complete": flow["verification_uri_complete"],
            "interval": flow["interval"],
            "expires_in": expires_in,
        }
    )


@app.post("/api/login/poll")
def api_login_poll():
    flow = load_json(DEVICE_FLOW_FILE, default=None) or {}
    if not flow.get("device_code"):
        return jsonify({"error": "no_device_flow"}), 400

    if time.time() > float(flow.get("expires_at", 0) or 0):
        return jsonify({"error": "expired"}), 400

    r = requests.post(
        TOKEN_URL,
        data={
            "client_id": CLIENT_ID,
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "device_code": flow["device_code"],
        },
        timeout=20,
    )

    # Not yet authorized -> typically 400 with JSON {"error":"authorization_pending"}
    if r.status_code >= 400:
        try:
            err = r.json().get("error", "unknown")
        except Exception:
            err = "unknown"
        return jsonify({"error": err}), 400

    tok = r.json()
    # Expect refresh_token, access_token, expires_in
    auth = load_json(AUTH_FILE, default={}) or {}
    auth.update(
        {
            "account_label": flow.get("account_label", ""),
            "access_token": tok.get("access_token"),
            "refresh_token": tok.get("refresh_token"),
            "expires_at": time.time() + int(tok.get("expires_in", 0) or 0),
        }
    )
    save_json(AUTH_FILE, auth)
    log.info("Auth stored (refresh_token present=%s)", bool(auth.get("refresh_token")))
    return jsonify({"ok": True})
