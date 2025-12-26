import os
import json
import time
import logging
from pathlib import Path
from flask import Flask, request, redirect, Response
import requests
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

log_level = os.getenv("LOG_LEVEL", "info").upper()
logging.basicConfig(level=getattr(logging, log_level, logging.INFO))
log = logging.getLogger("tado-assistant")

app = Flask(__name__, static_folder="/static", static_url_path="/static")


def _load_json(path: Path, default=None):
    try:
        if not path.exists():
            return default
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("load_json failed for %s: %s", path, e)
        return default


def _save_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _token_ok(auth: dict) -> bool:
    try:
        exp = float(auth.get("expires_at", 0) or 0)
    except Exception:
        exp = 0.0
    return bool(auth.get("access_token")) and exp > time.time() + 30


def _html(body: str) -> str:
    css = """
    <style>
      body { font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif; margin: 18px; }
      .wrap { max-width: 900px; margin: 0 auto; }
      .top { display:flex; align-items:center; justify-content:space-between; gap:16px; margin-bottom:18px; }
      .card { border: 1px solid #ddd; border-radius: 12px; padding: 16px; margin: 14px 0; }
      .muted { color: #666; }
      .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace; }
      button { padding: 10px 14px; border-radius: 10px; border: 1px solid #ccc; background: #f8f8f8; cursor:pointer; }
      button.primary { background: #111; color:#fff; border-color:#111; }
      a { color: inherit; }
      .ok { color: #0a7; font-weight: 700; }
      .bad { color: #c22; font-weight: 700; }
      .row { display:flex; gap:10px; flex-wrap:wrap; align-items:center; }
      .code { display:inline-block; padding: 6px 10px; border:1px solid #ddd; border-radius:10px; background:#fafafa; }
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
    auth = _load_json(AUTH_FILE, default={}) or {}
    ok = _token_ok(auth)

    exp = 0.0
    try:
        exp = float(auth.get("expires_at", 0) or 0)
    except Exception:
        exp = 0.0

    exp_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(exp)) if exp else "—"
    email = auth.get("account_label") or "—"

    flow = _load_json(DEVICE_FLOW_FILE, default={}) or {}
    user_code = flow.get("user_code")
    verification_uri = flow.get("verification_uri_complete") or flow.get("verification_uri")

    status_line = "<span class='ok'>✅ Token OK</span>" if ok else "<span class='bad'>❌ Kein/abgelaufener Token</span>"
    flow_block = ""
    if user_code and verification_uri:
        flow_block = f"""
        <div class="card">
          <h3 style="margin-top:0;">Login Schritt</h3>
          <div class="muted">Öffne diesen Link und gib den Code ein:</div>
          <div class="row" style="margin-top:10px;">
            <a class="code mono" href="{verification_uri}" target="_blank" rel="noopener noreferrer">{verification_uri}</a>
            <span class="code mono">Code: {user_code}</span>
          </div>
          <div class="muted" style="margin-top:10px;">Danach: <b>Token abrufen (poll)</b>.</div>
        </div>
        """

    body = f"""
    <div class="card">
      <h2 style="margin-top:0;">Status</h2>
      <div>{status_line}</div>
      <div class="muted" style="margin-top:8px;">gültig bis: {exp_str}</div>
      <div class="muted" style="margin-top:4px;">Konto-Label: {email}</div>
    </div>

    <div class="card">
      <h2 style="margin-top:0;">Login (Device Code Flow)</h2>
      <div class="row" style="margin-top:12px;">
        <form method="post" action="/auth/start"><button class="primary" type="submit">Login starten</button></form>
        <form method="post" action="/auth/poll"><button type="submit">Token abrufen (poll)</button></form>
        <form method="post" action="/auth/clear"><button type="submit">Token löschen</button></form>
      </div>
    </div>

    {flow_block}
    """
    return Response(_html(body), mimetype="text/html")


@app.post("/auth/start")
def auth_start():
    payload = {"client_id": CLIENT_ID, "scope": SCOPE}
    r = requests.post(DEVICE_AUTHORIZE_URL, data=payload, timeout=30)
    r.raise_for_status()
    flow = r.json()
    flow["account_label"] = (request.form.get("account_label") or "").strip()
    _save_json(DEVICE_FLOW_FILE, flow)
    return redirect("/", code=302)


@app.post("/auth/poll")
def auth_poll():
    flow = _load_json(DEVICE_FLOW_FILE, default={}) or {}
    device_code = flow.get("device_code")
    if not device_code:
        return redirect("/", code=302)

    payload = {
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        "client_id": CLIENT_ID,
        "device_code": device_code,
    }
    r = requests.post(TOKEN_URL, data=payload, timeout=30)

    if r.status_code != 200:
        try:
            err = r.json()
        except Exception:
            err = {"raw": r.text}
        log.warning("token poll failed: %s", err)
        return redirect("/", code=302)

    tok = r.json()
    auth = _load_json(AUTH_FILE, default={}) or {}
    auth.update(
        {
            "account_label": flow.get("account_label", "") or auth.get("account_label", ""),
            "access_token": tok.get("access_token"),
            "refresh_token": tok.get("refresh_token") or auth.get("refresh_token"),
            "expires_at": time.time() + int(tok.get("expires_in", 0) or 0),
        }
    )
    _save_json(AUTH_FILE, auth)
    log.info("Auth stored (refresh_token present=%s)", bool(auth.get("refresh_token")))
    return redirect("/", code=302)


@app.post("/auth/clear")
def auth_clear():
    try:
        if AUTH_FILE.exists():
            AUTH_FILE.unlink()
    except Exception:
        pass
    try:
        if DEVICE_FLOW_FILE.exists():
            DEVICE_FLOW_FILE.unlink()
    except Exception:
        pass
    return redirect("/", code=302)


@app.get("/api/status")
def api_status():
    auth = _load_json(AUTH_FILE, default={}) or {}
    return {
        "token_ok": _token_ok(auth),
        "expires_at": auth.get("expires_at"),
        "account_label": auth.get("account_label"),
    }


if __name__ == "__main__":
    os.environ["FLASK_ENV"] = "production"
    os.environ["FLASK_DEBUG"] = "0"
    os.environ["WERKZEUG_DEBUG_PIN"] = "off"

    host = os.getenv("HOST", "0.0.0.0") or "0.0.0.0"
    port = int(os.getenv("PORT", "8099") or "8099")

    app.debug = False
    app.config["ENV"] = "production"

    log.info("Starting web UI on %s:%s", host, port)
    run_simple(hostname=host, port=port, application=app, use_reloader=False, use_debugger=False, threaded=True)
