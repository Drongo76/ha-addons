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
AUTH_BASE = "https://auth.tado.com/oauth"
DEVICE_AUTHORIZE_URL = f"{AUTH_BASE}/device_authorize"
TOKEN_URL = f"{AUTH_BASE}/token"

# Client ID wird von tado-web-app verwendet (Device Flow)
CLIENT_ID = os.getenv("TADO_CLIENT_ID", "tado-web-app")
SCOPE = os.getenv("TADO_SCOPE", "offline_access")

log_level = os.getenv("LOG_LEVEL", "info").upper()
logging.basicConfig(level=getattr(logging, log_level, logging.INFO))
log = logging.getLogger("tado-assistant")

app = Flask(__name__, static_folder="/static", static_url_path="/static")


def load_json(path: Path, default=None):
    try:
        if not path.exists():
            return default
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("load_json failed for %s: %s", path, e)
        return default


def save_json(path: Path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def token_status():
    auth = load_json(AUTH_FILE, default={}) or {}
    exp = float(auth.get("expires_at", 0) or 0)
    now = time.time()
    ok = bool(auth.get("access_token")) and exp > now + 30
    return ok, exp, auth


def html_page(body: str):
    css = """
    <style>
      body { font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif; margin: 20px; }
      .wrap { max-width: 900px; margin: 0 auto; }
      .top { display:flex; align-items:center; justify-content:space-between; gap:16px; margin-bottom:18px; }
      .card { border: 1px solid #ddd; border-radius: 12px; padding: 16px; margin: 14px 0; }
      .muted { color: #666; }
      .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace; }
      button { padding: 10px 14px; border-radius: 10px; border: 1px solid #ccc; background: #f8f8f8; cursor:pointer; }
      button.primary { background: #111; color:#fff; border-color:#111; }
      a { color: inherit; }
      .ok { color: #0a7; font-weight: 600; }
      .bad { color: #c22; font-weight: 600; }
    </style>
    """
    header = """
    <div class="top">
      <div>
        <h2 style="margin:0;">Tado Assistant (Ingress)</h2>
        <div class="muted">Device Code Flow Login + Presence Worker</div>
      </div>
      <img src="/static/tado.svg" alt="tado" style="height:28px; opacity:.9"/>
    </div>
    """
    return f"<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'>{css}</head><body><div class='wrap'>{header}{body}</div></body></html>"


@app.get("/")
def index():
    ok, exp, auth = token_status()
    email = auth.get("account_label", "")
    exp_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(exp)) if exp else "—"

    status_line = "<span class='ok'>✅ Token OK</span>" if ok else "<span class='bad'>❌ Kein/abgelaufener Token</span>"
    home_line = f"<span class='muted'>gültig bis: {exp_str}</span>" if exp else ""

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

      <div style="margin-top:12px; display:flex; gap:10px; flex-wrap:wrap;">
        <form method="post" action="/api/device/start"><button class="primary" type="submit">Login starten</button></form>
        <form method="post" action="/api/device/poll"><button type="submit">Token abrufen (poll)</button></form>
      </div>
    </div>
    """
    return Response(html_page(body), mimetype="text/html")


@app.post("/api/device/start")
def device_start():
    payload = {
        "client_id": CLIENT_ID,
        "scope": SCOPE,
    }
    r = requests.post(DEVICE_AUTHORIZE_URL, data=payload, timeout=20)
    r.raise_for_status()
    flow = r.json()

    # optional: account_label vom user (nur Anzeige)
    flow["account_label"] = request.form.get("account_label", "")

    save_json(DEVICE_FLOW_FILE, flow)
    return jsonify(flow)


@app.post("/api/device/poll")
def device_poll():
    flow = load_json(DEVICE_FLOW_FILE, default={}) or {}
    device_code = flow.get("device_code")
    if not device_code:
        return jsonify({"error": "no_device_code"}), 400

    payload = {
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        "client_id": CLIENT_ID,
        "device_code": device_code,
    }
    r = requests.post(TOKEN_URL, data=payload, timeout=20)

    if r.status_code != 200:
        try:
            err = r.json()
        except Exception:
            err = {"raw": r.text}
        return jsonify({"error": err}), 400

    tok = r.json()
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


# ---- DAS fehlende Ende: Server-Start ohne Debug/ReLoader ----
if __name__ == "__main__":
    # HA Add-on / s6: niemals Werkzeug-ReLoader oder Debug-Fork benutzen,
    # sonst beendet sich PID 1 und das Add-on startet in einer Schleife neu.
    os.environ["FLASK_ENV"] = "production"
    os.environ["FLASK_DEBUG"] = "0"

    port = int(os.getenv("PORT", "8099") or "8099")
    host = os.getenv("HOST", "0.0.0.0")

    print(f"[tado-assistant] starting web ui on {host}:{port} (debug=False, use_reloader=False)", flush=True)
    app.run(host=host, port=port, debug=False, use_reloader=False, threaded=True)
