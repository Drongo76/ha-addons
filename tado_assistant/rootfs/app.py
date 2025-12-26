import json
import os
import time
from datetime import datetime, timezone

import requests
from flask import Flask, redirect, request

app = Flask(__name__)

# Tado Device Code Flow (offiziell)
TADO_CLIENT_ID = "1bb50063-6b0c-4d11-bd99-387f4a91cc46"
DEVICE_AUTHORIZE_URL = "https://login.tado.com/oauth2/device_authorize"
TOKEN_URL = "https://login.tado.com/oauth2/token"

DATA_DIR = "/data"
FLOW_FILE = os.path.join(DATA_DIR, "tado_device_flow.json")
TOKEN_FILE = os.path.join(DATA_DIR, "tado_tokens.json")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_json(path: str):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except Exception:
        return None


def _write_json(path: str, obj: dict):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def _delete_file(path: str):
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def _html_page(title: str, body: str) -> str:
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <style>
    body {{ font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial; margin: 24px; }}
    .card {{ max-width: 760px; padding: 18px; border: 1px solid #ddd; border-radius: 12px; }}
    code {{ background: #f6f6f6; padding: 2px 6px; border-radius: 6px; }}
    a.button, button {{ display: inline-block; padding: 10px 14px; border-radius: 10px; border: 1px solid #333;
                       background: #111; color: #fff; text-decoration: none; cursor: pointer; }}
    button.secondary, a.secondary {{ background: #fff; color: #111; }}
    .row {{ margin-top: 12px; }}
    .mono {{ font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace; }}
    .muted {{ color: #666; }}
    .error {{ color: #b00020; font-weight: 600; }}
    .ok {{ color: #0a7a2f; font-weight: 600; }}
  </style>
</head>
<body>
  <div class="card">
    <h2>{title}</h2>
    {body}
  </div>
</body>
</html>"""


@app.get("/")
def index():
    tokens = _read_json(TOKEN_FILE)
    flow = _read_json(FLOW_FILE)

    if tokens and tokens.get("refresh_token"):
        body = f"""
        <p class="ok">✅ Eingeloggt</p>
        <p class="muted">Token gespeichert in <code>/data/tado_tokens.json</code></p>
        <div class="row">
          <form method="post" action="/auth/logout">
            <button type="submit" class="secondary">Logout (Tokens löschen)</button>
          </form>
        </div>
        """
        return _html_page("Tado Assistant (Ingress)", body)

    if flow and flow.get("verification_uri_complete"):
        body = f"""
        <p>🔐 Login läuft bereits. Öffne den Link auf Handy/PC und bestätige.</p>
        <p><a class="button" href="{flow["verification_uri_complete"]}" target="_blank" rel="noreferrer">Tado Login öffnen</a></p>
        <p>Code: <span class="mono"><b>{flow.get("user_code","")}</b></span></p>
        <div class="row">
          <form method="post" action="/auth/poll">
            <button type="submit">Ich habe bestätigt → Token holen</button>
          </form>
        </div>
        <div class="row">
          <form method="post" action="/auth/reset">
            <button type="submit" class="secondary">Flow zurücksetzen</button>
          </form>
        </div>
        """
        return _html_page("Tado Assistant (Ingress)", body)

    body = """
    <p>Starte den offiziellen Tado Device-Code Login.</p>
    <div class="row">
      <form method="post" action="/auth/start">
        <button type="submit">Tado Login starten</button>
      </form>
    </div>
    """
    return _html_page("Tado Assistant (Ingress)", body)


@app.post("/auth/start")
def auth_start():
    # Device Authorize
    try:
        r = requests.post(
            DEVICE_AUTHORIZE_URL,
            params={"client_id": TADO_CLIENT_ID, "scope": "offline_access"},
            timeout=20,
        )
        data = r.json()
    except Exception as e:
        return _html_page("Fehler", f'<p class="error">Device authorize fehlgeschlagen: {e}</p>')

    if not isinstance(data, dict) or "device_code" not in data:
        return _html_page("Fehler", f'<p class="error">Unerwartete Antwort: {data}</p>')

    data["_created_at"] = _now_iso()
    _write_json(FLOW_FILE, data)
    return redirect("/", code=302)


@app.post("/auth/poll")
def auth_poll():
    flow = _read_json(FLOW_FILE)
    if not flow or "device_code" not in flow:
        return redirect("/", code=302)

    device_code = flow["device_code"]
    interval = int(flow.get("interval", 5))

    # Ein Poll-Versuch (kein Endlos-Poll im Webrequest)
    try:
        r = requests.post(
            TOKEN_URL,
            params={
                "client_id": TADO_CLIENT_ID,
                "device_code": device_code,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            },
            timeout=20,
        )
        data = r.json()
    except Exception as e:
        return _html_page("Fehler", f'<p class="error">Token-Request fehlgeschlagen: {e}</p>')

    # Erfolg
    if isinstance(data, dict) and data.get("access_token"):
        tokens = {
            **data,
            "_obtained_at": _now_iso(),
        }
        _write_json(TOKEN_FILE, tokens)
        _delete_file(FLOW_FILE)
        return redirect("/", code=302)

    # Noch nicht bestätigt / Fehler
    err = ""
    if isinstance(data, dict):
        err = data.get("error", "") or data.get("error_description", "")
    err = err or str(data)

    hint = f"""
    <p class="error">Noch kein Token: <span class="mono">{err}</span></p>
    <p class="muted">Tipp: Wenn du den Login gerade erst bestätigt hast, warte {interval} Sekunden und klicke nochmal.</p>
    <div class="row">
      <form method="post" action="/auth/poll">
        <button type="submit">Nochmal Token holen</button>
      </form>
    </div>
    <div class="row">
      <a class="button secondary" href="/" >Zurück</a>
    </div>
    """
    return _html_page("Tado Login Status", hint)


@app.post("/auth/reset")
def auth_reset():
    _delete_file(FLOW_FILE)
    return redirect("/", code=302)


@app.post("/auth/logout")
def auth_logout():
    _delete_file(TOKEN_FILE)
    _delete_file(FLOW_FILE)
    return redirect("/", code=302)
