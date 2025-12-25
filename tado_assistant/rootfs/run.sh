#!/usr/bin/env bash
set -euo pipefail

mkdir -p /data/tado_assistant

cat >/app.py <<'PY'
import json
import os
import time
import requests
from flask import Flask, request, redirect

APP_DIR = "/data/tado_assistant"
AUTH_FILE = os.path.join(APP_DIR, "auth.json")
PENDING_FILE = os.path.join(APP_DIR, "pending.json")

TADO_CLIENT_ID = "1bb50063-6b0c-4d11-bd99-387f4a91cc46"
DEVICE_AUTHORIZE_URL = "https://login.tado.com/oauth2/device_authorize"
TOKEN_URL = "https://login.tado.com/oauth2/token"
ME_URL = "https://my.tado.com/api/v2/me"

app = Flask(__name__, static_folder="/static", static_url_path="/static")


def _read_json(path: str):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _write_json(path: str, data: dict):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def load_auth():
    return _read_json(AUTH_FILE) or {}


def load_pending():
    return _read_json(PENDING_FILE) or {}


def auth_status():
    a = load_auth()
    ok = bool(a.get("refresh_token")) and bool(a.get("home_id"))
    return ok, a.get("home_id"), a.get("email_label", "")


def get_home_id(access_token: str):
    r = requests.get(ME_URL, headers={"Authorization": f"Bearer {access_token}"}, timeout=25)
    r.raise_for_status()
    data = r.json()
    homes = data.get("homes") or []
    if not homes:
        raise RuntimeError("Keine homes in /api/v2/me gefunden.")
    return homes[0]["id"]


def page(title: str, body_html: str):
    # Wichtig: Links/Form-Actions RELATIV (Ingress!)
    return f"""<!doctype html>
<html lang="de">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{title}</title>
  <style>
    body {{ font-family: -apple-system, system-ui, Segoe UI, Roboto, Arial, sans-serif; margin: 0; background: #0b0b0c; color: #f4f4f5; }}
    .wrap {{ max-width: 820px; margin: 0 auto; padding: 22px; }}
    .card {{ background: #141416; border: 1px solid #26262a; border-radius: 16px; padding: 18px; }}
    .row {{ display: flex; gap: 12px; flex-wrap: wrap; align-items: center; }}
    .btn {{ display:inline-block; padding: 10px 14px; border-radius: 12px; border: 1px solid #2f2f36; background: #1b1b20; color:#fff; text-decoration:none; cursor:pointer; }}
    .btn.primary {{ background:#e54b2c; border-color:#e54b2c; }}
    input {{ padding: 10px 12px; border-radius: 12px; border:1px solid #2f2f36; background:#0f0f12; color:#fff; width: 100%; max-width: 420px; }}
    code {{ background:#0f0f12; padding: 2px 6px; border-radius: 8px; border:1px solid #2a2a30; }}
    .muted {{ color:#b1b1bb; }}
    .ok {{ color:#5eead4; }}
    .warn {{ color:#fbbf24; }}
    a {{ color:#93c5fd; }}
    hr {{ border:none; border-top:1px solid #26262a; margin:18px 0; }}
  </style>
</head>
<body>
  <div class="wrap">
    <div style="margin-bottom: 14px;">
      <img src="static/tado.svg" alt="tado" style="height:70px; display:block; margin-bottom:10px;" />
      <div class="muted">Tado Assistant Add-on (Ingress)</div>
    </div>
    <div class="card">
      {body_html}
    </div>
  </div>
</body>
</html>"""


@app.get("/")
def index():
    ok, home_id, email_label = auth_status()
    status = f'<span class="ok">OK</span>' if ok else f'<span class="warn">Nicht eingerichtet</span>'
    home = f"<code>{home_id}</code>" if home_id else "<span class='muted'>—</span>"
    label = f"<code>{email_label}</code>" if email_label else "<span class='muted'>—</span>"

    body = f"""
      <h2 style="margin:0 0 10px 0;">Status</h2>
      <div class="row" style="margin-bottom:14px;">
        <div>Auth: {status}</div>
        <div class="muted">Home ID: {home}</div>
        <div class="muted">Konto-Label: {label}</div>
      </div>

      <h3 style="margin:18px 0 8px 0;">Login (Device Code Flow)</h3>
      <p class="muted" style="margin-top:0;">
        Du startest hier den Login, bekommst Link + Code, bestätigst bei tado,
        dann holst du hier das Token ab.
      </p>

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

      <h3 style="margin:0 0 8px 0;">MQTT</h3>
      <p class="muted" style="margin-top:0;">
        MQTT wird erst aktiv, wenn Auth OK + Home ID vorhanden ist.
      </p>
    """
    return page("Tado Assistant", body)


@app.post("/auth/start")
def auth_start():
    email_label = (request.form.get("email_label") or "").strip()

    r = requests.post(
        DEVICE_AUTHORIZE_URL,
        params=dict(client_id=TADO_CLIENT_ID, scope="offline_access"),
        timeout=25,
    )
    r.raise_for_status()
    payload = r.json()

    pending = {
        "created": int(time.time()),
        "device_code": payload.get("device_code"),
        "user_code": payload.get("user_code"),
        "verification_uri": payload.get("verification_uri"),
        "verification_uri_complete": payload.get("verification_uri_complete"),
        "interval": int(payload.get("interval") or 5),
        "expires_in": int(payload.get("expires_in") or 300),
        "email_label": email_label,
    }
    _write_json(PENDING_FILE, pending)

    return redirect("..")  # relativ


@app.get("/auth")
def auth_page():
    p = load_pending()
    ok, home_id, email_label = auth_status()

    if not p.get("device_code"):
        body = """
          <h2 style="margin:0 0 10px 0;">Login-Status</h2>
          <p class="muted">Noch kein Login gestartet.</p>
          <a class="btn" href="../">Zurück</a>
        """
        return page("Login", body)

    now = int(time.time())
    created = int(p.get("created") or now)
    expires_in = int(p.get("expires_in") or 300)
    remaining = max(0, expires_in - (now - created))

    vurl = p.get("verification_uri_complete") or p.get("verification_uri") or ""
    ucode = p.get("user_code") or ""
    interval = int(p.get("interval") or 5)

    body = f"""
      <h2 style="margin:0 0 10px 0;">Login-Status</h2>
      <div class="muted" style="margin-bottom:14px;">Restzeit: <code>{remaining}</code> Sekunden</div>

      <div class="card" style="padding:14px; border-radius:14px;">
        <div style="margin-bottom:8px;">1) Link öffnen und bei tado bestätigen:</div>
        <div style="margin-bottom:10px;"><a href="{vurl}" target="_blank" rel="noreferrer">{vurl}</a></div>
        <div>2) Falls nötig Code eingeben: <code style="font-size:18px;">{ucode}</code></div>
      </div>

      <form method="post" action="auth/poll" style="margin-top:14px;">
        <button class="btn primary" type="submit">Token abrufen</button>
        <a class="btn" href="../">Zurück</a>
      </form>

      <p class="muted" style="margin-top:14px;">
        Bitte nicht schneller als alle <code>{interval}</code> Sekunden drücken.
      </p>

      <hr />
      <div class="muted">Aktueller Status: Auth={'OK' if ok else 'nicht eingerichtet'} / Home ID={home_id or '—'} / Konto-Label={email_label or '—'}</div>
    """
    return page("Login", body)


@app.post("/auth/poll")
def auth_poll():
    p = load_pending()
    device_code = p.get("device_code")
    if not device_code:
        return redirect("..")

    r = requests.post(
        TOKEN_URL,
        params=dict(
            client_id=TADO_CLIENT_ID,
            device_code=device_code,
            grant_type="urn:ietf:params:oauth:grant-type:device_code",
        ),
        timeout=25,
    )

    if r.status_code != 200:
        body = f"""
          <h2 style="margin:0 0 10px 0;">Noch nicht bestätigt</h2>
          <p class="muted">Bei tado noch nicht final bestätigt oder zu früh gepollt.</p>
          <pre style="white-space:pre-wrap; background:#0f0f12; padding:12px; border-radius:12px; border:1px solid #2a2a30;">{r.text}</pre>
          <a class="btn primary" href="../auth">Zurück</a>
        """
        return page("Warten", body)

    token = r.json()
    refresh_token = token.get("refresh_token")
    access_token = token.get("access_token")
    if not refresh_token or not access_token:
        return page("Fehler", "<h2>Fehler</h2><p class='muted'>Token-Antwort unvollständig.</p><a class='btn' href='../auth'>Zurück</a>")

    home_id = get_home_id(access_token)

    auth = {
        "email_label": p.get("email_label", ""),
        "home_id": home_id,
        "refresh_token": refresh_token,
        "saved": int(time.time()),
    }
    _write_json(AUTH_FILE, auth)

    try:
        os.remove(PENDING_FILE)
    except FileNotFoundError:
        pass

    return redirect("../..")  # zurück zur Startseite


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8099)
PY

# Worker starten (wichtig für MQTT)
if [ -f /worker.py ]; then
  python3 /worker.py &
fi

exec python3 /app.py
