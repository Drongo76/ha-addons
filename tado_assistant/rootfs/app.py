import os
import json
import time
import logging
from pathlib import Path

import requests
from flask import Flask, request, redirect, Response
from werkzeug.serving import run_simple

DATA_DIR = Path("/data")
DATA_DIR.mkdir(parents=True, exist_ok=True)

AUTH_FILE = DATA_DIR / "tado_auth.json"
DEVICE_FLOW_FILE = DATA_DIR / "device_flow.json"

AUTH_BASE = "https://auth.tado.com/oauth"
DEVICE_AUTHORIZE_URL = f"{AUTH_BASE}/device_authorize"
TOKEN_URL = f"{AUTH_BASE}/token"

CLIENT_ID = os.getenv("TADO_CLIENT_ID", "tado-web-app")
SCOPE = os.getenv("TADO_SCOPE", "offline_access")

LOG_LEVEL = os.getenv("LOG_LEVEL", "info").upper()
logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO))
log = logging.getLogger("tado-assistant")

app = Flask(__name__, static_folder="/static", static_url_path="/static")
app.debug = False
app.config["ENV"] = "production"
app.config["DEBUG"] = False


def load_json(path: Path, default=None):
    try:
        if not path.exists():
            return default
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path: Path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def token_status():
    auth = load_json(AUTH_FILE, default={}) or {}
    exp = float(auth.get("expires_at", 0) or 0)
    ok = bool(auth.get("access_token")) and exp > time.time() + 30
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
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"{css}</head><body><div class='wrap'>{header}{body}</div></body></html>"
    )


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
      <div style="margin-top:12px; display:flex; gap:10px; flex-wrap:wrap;">
        <form method="post" action="/api/device/start"><button class="primary" type="submit">Login starten</button></form>
        <form method="post" action="/api/device/poll"><button type="submit">Token abrufen (poll)</button></form>
      </div>
    </div>
    """
    return Response(html_page(body), mimetype="text/html")


@app.post("/api/device/start")
def device_start():
    payload = {"client_id": CLIENT_ID, "scope": SCOPE}
    r = requests.post(DEVICE_AUTHORIZE_URL, data=payload, timeout=20)
    r.raise_for_status()
    flow = r.json()
    flow["account_label"] = request.form.get("account_label", "")
    save_json(DEVICE_FLOW_FILE, flow)
    return Response(json.dumps(flow), mimetype="application/json")


@app.post("/api/device/poll")
def device_poll():
    flow = load_json(DEVICE_FLOW_FILE, default={}) or {}
    device_code = flow.get("device_code")
    if not device_code:
        return Response(json.dumps({"error": "no_device_code"}), status=400, mimetype="application/json")

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
        return Response(json.dumps({"error": err}), status=400, mimetype="application/json")

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
    return Response(json.dumps({"ok": True}), mimetype="application/json")


if __name__ == "__main__":
    os.environ["FLASK_ENV"] = "production"
    os.environ["FLASK_DEBUG"] = "0"
    os.environ["WERKZEUG_DEBUG_PIN"] = "off"

    host = os.getenv("HOST", "0.0.0.0") or "0.0.0.0"
    port = int(os.getenv("PORT", "8099") or "8099")

    run_simple(
        hostname=host,
        port=port,
        application=app,
        use_reloader=False,
        use_debugger=False,
        threaded=True,
    )
