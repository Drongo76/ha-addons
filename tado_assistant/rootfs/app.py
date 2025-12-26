#!/usr/bin/env python3
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import requests
from flask import Flask, redirect, render_template_string, request, url_for

APP_NAME = "Tado Assistant (Ingress)"
DATA_DIR = os.getenv("DATA_DIR", "/data")
TOKENS_PATH = os.path.join(DATA_DIR, "tado_tokens.json")

# Tado OAuth (Device Code Flow)
# Du kannst das über ENV überschreiben, falls du es anders brauchst.
TADO_DEVICE_CODE_URL = os.getenv(
    "TADO_DEVICE_CODE_URL", "https://auth.tado.com/oauth/device_authorize"
)
TADO_TOKEN_URL = os.getenv("TADO_TOKEN_URL", "https://auth.tado.com/oauth/token")
TADO_CLIENT_ID = os.getenv("TADO_CLIENT_ID", "tado-web-app")  # häufig genutzter Client
TADO_SCOPE = os.getenv("TADO_SCOPE", "offline_access")

# Flask
app = Flask(__name__)

# Logging
_log_level = os.getenv("LOG_LEVEL", "info").upper()
logging.basicConfig(level=getattr(logging, _log_level, logging.INFO))
log = logging.getLogger("tado-assistant-ui")

# Ingress/Web Port
PORT = int(os.getenv("PORT", "8099") or "8099")

# In-memory state for ongoing device flow
_device_flow_state: Dict[str, Any] = {}


def _ensure_data_dir() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)


def _load_tokens() -> Optional[Dict[str, Any]]:
    try:
        with open(TOKENS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except Exception as e:
        log.exception("Failed to load tokens: %s", e)
        return None


def _save_tokens(tokens: Dict[str, Any]) -> None:
    _ensure_data_dir()
    with open(TOKENS_PATH, "w", encoding="utf-8") as f:
        json.dump(tokens, f, indent=2, ensure_ascii=False)


def _delete_tokens() -> None:
    try:
        os.remove(TOKENS_PATH)
    except FileNotFoundError:
        pass


def _token_is_valid(tokens: Dict[str, Any]) -> bool:
    # We store expires_at as ISO or epoch; accept both
    expires_at = tokens.get("expires_at")
    if not expires_at:
        return False

    try:
        if isinstance(expires_at, (int, float)):
            dt = datetime.fromtimestamp(float(expires_at), tz=timezone.utc)
        else:
            dt = datetime.fromisoformat(str(expires_at))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        # give a small safety window
        return dt > datetime.now(timezone.utc) + timedelta(seconds=30)
    except Exception:
        return False


def _human_dt(tokens: Dict[str, Any]) -> str:
    expires_at = tokens.get("expires_at")
    if not expires_at:
        return "-"
    try:
        if isinstance(expires_at, (int, float)):
            dt = datetime.fromtimestamp(float(expires_at), tz=timezone.utc)
        else:
            dt = datetime.fromisoformat(str(expires_at))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    except Exception:
        return str(expires_at)


def _start_device_code_flow() -> Dict[str, Any]:
    """
    Calls Tado device_authorize endpoint.
    Returns dict with device_code/user_code/verification_uri/_complete/interval/expires_in
    """
    payload = {
        "client_id": TADO_CLIENT_ID,
        "scope": TADO_SCOPE,
    }

    log.info("Starting device code flow against %s", TADO_DEVICE_CODE_URL)
    r = requests.post(TADO_DEVICE_CODE_URL, data=payload, timeout=20)
    r.raise_for_status()
    data = r.json()

    # normalize some fields
    interval = int(data.get("interval", 5))
    expires_in = int(data.get("expires_in", 600))
    data["interval"] = interval
    data["expires_in"] = expires_in

    # keep state for polling
    _device_flow_state.clear()
    _device_flow_state.update(
        {
            "started_at": time.time(),
            "device_code": data.get("device_code"),
            "interval": interval,
            "expires_in": expires_in,
            "last_poll_at": 0.0,
        }
    )
    return data


def _poll_device_code_for_token() -> Dict[str, Any]:
    """
    Polls token endpoint once.
    On success, returns token payload and saves it to /data.
    On pending/slow_down, returns {"status": "..."}.
    On errors, returns {"error": "...", "detail": "..."}.
    """
    device_code = _device_flow_state.get("device_code")
    if not device_code:
        return {"error": "no_device_flow", "detail": "Device-Code Flow wurde nicht gestartet."}

    # enforce polling interval (avoid hammering)
    interval = int(_device_flow_state.get("interval", 5))
    now = time.time()
    last = float(_device_flow_state.get("last_poll_at", 0.0))
    if now - last < max(1, interval):
        return {"status": "wait", "detail": f"Bitte warten (Interval {interval}s)."}

    # check expiry
    started_at = float(_device_flow_state.get("started_at", now))
    expires_in = int(_device_flow_state.get("expires_in", 600))
    if now - started_at > expires_in:
        _device_flow_state.clear()
        return {"error": "expired", "detail": "Device-Code Flow ist abgelaufen. Bitte neu starten."}

    _device_flow_state["last_poll_at"] = now

    payload = {
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        "client_id": TADO_CLIENT_ID,
        "device_code": device_code,
    }

    r = requests.post(TADO_TOKEN_URL, data=payload, timeout=20)

    # Tado returns 400 with json error for pending etc.
    try:
        data = r.json()
    except Exception:
        data = {"raw": r.text}

    if r.status_code == 200 and "access_token" in data:
        # compute expires_at
        expires_in_token = int(data.get("expires_in", 0) or 0)
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in_token)

        tokens = {
            "access_token": data.get("access_token"),
            "refresh_token": data.get("refresh_token"),
            "token_type": data.get("token_type"),
            "scope": data.get("scope"),
            "expires_in": expires_in_token,
            "expires_at": expires_at.isoformat(),
            "obtained_at": datetime.now(timezone.utc).isoformat(),
        }
        _save_tokens(tokens)
        _device_flow_state.clear()
        return {"status": "ok", "detail": "Token erhalten und gespeichert.", "tokens_saved": True}

    # Handle known device flow errors
    err = data.get("error") if isinstance(data, dict) else None
    if err in ("authorization_pending", "slow_down"):
        return {"status": err, "detail": "Noch nicht bestätigt. Bitte in Tado freigeben."}
    if err in ("access_denied", "expired_token", "invalid_grant"):
        return {"error": err, "detail": "Abgebrochen/abgelaufen. Bitte neu starten."}

    return {
        "error": "token_request_failed",
        "detail": f"Token Request fehlgeschlagen (HTTP {r.status_code}).",
        "response": data,
    }


PAGE = """
<!doctype html>
<html lang="de">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{{ title }}</title>
  <style>
    body { font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif; margin: 20px; }
    .card { border: 1px solid #ddd; border-radius: 12px; padding: 16px; margin: 14px 0; }
    .row { display:flex; gap: 10px; flex-wrap: wrap; align-items: center; }
    .muted { color: #666; }
    .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace; }
    button { padding: 10px 14px; border-radius: 10px; border: 1px solid #ccc; background: #f8f8f8; cursor:pointer; }
    button.primary { background: #111; color:#fff; border-color:#111; }
    a.btn { display:inline-block; padding: 10px 14px; border-radius: 10px; border: 1px solid #ccc; text-decoration:none; color:inherit; background:#f8f8f8;}
    .ok { color: #0a7; font-weight:600; }
    .bad { color: #c22; font-weight:600; }
    pre { background:#fafafa; padding:12px; border-radius: 10px; overflow:auto; }
  </style>
</head>
<body>
  <h2>{{ title }}</h2>
  <div class="muted">Ingress UI für Login/Status. Tokens werden in <span class="mono">{{ tokens_path }}</span> gespeichert.</div>

  <div class="card">
    <h3>Status</h3>
    {% if tokens and token_valid %}
      <div class="ok">✅ Token vorhanden (gültig bis {{ expires_at }})</div>
    {% elif tokens %}
      <div class="bad">⚠️ Token vorhanden, aber abgelaufen/ungültig</div>
    {% else %}
      <div class="bad">❌ Kein Token gespeichert</div>
    {% endif %}
    <div class="row" style="margin-top:10px;">
      <a class="btn" href="{{ url_for('index') }}">Neu laden</a>
      <form method="post" action="{{ url_for('logout') }}" style="margin:0;">
        <button type="submit">Logout / Token löschen</button>
      </form>
    </div>
  </div>

  <div class="card">
    <h3>Tado Login (Device Code Flow)</h3>
    <div class="row">
      <form method="post" action="{{ url_for('auth_start') }}" style="margin:0;">
        <button class="primary" type="submit">Login starten</button>
      </form>
      <form method="post" action="{{ url_for('auth_poll') }}" style="margin:0;">
        <button type="submit">Token abrufen (poll)</button>
      </form>
    </div>

    {% if flow %}
      <hr>
      <div><b>User Code:</b> <span class="mono">{{ flow.user_code }}</span></div>
      <div class="muted">Öffne Link und bestätige: </div>
      {% if flow.verification_uri_complete %}
        <div><a href="{{ flow.verification_uri_complete }}">{{ flow.verification_uri_complete }}</a></div>
      {% else %}
        <div><a href="{{ flow.verification_uri }}">{{ flow.verification_uri }}</a></div>
      {% endif %}
      <div class="muted">Polling-Intervall: {{ flow.interval }}s • Läuft ab in {{ flow.expires_in }}s</div>
    {% endif %}

    {% if message %}
      <hr>
      <pre>{{ message }}</pre>
    {% endif %}
  </div>

  <div class="card">
    <h3>Hinweis</h3>
    <div class="muted">
      Der Worker liest MQTT-Host/User/Pass aus Add-on Optionen (über run.sh → ENV). Keine Passwörter im Repo.
    </div>
  </div>
</body>
</html>
"""


@app.get("/")
def index():
    tokens = _load_tokens()
    token_valid = bool(tokens and _token_is_valid(tokens))
    return render_template_string(
        PAGE,
        title=APP_NAME,
        tokens=tokens,
        token_valid=token_valid,
        expires_at=_human_dt(tokens) if tokens else "-",
        flow=None,
        message=None,
        tokens_path=TOKENS_PATH,
    )


@app.post("/auth/start")
def auth_start():
    try:
        flow = _start_device_code_flow()
        return render_template_string(
            PAGE,
            title=APP_NAME,
            tokens=_load_tokens(),
            token_valid=False if not _load_tokens() else _token_is_valid(_load_tokens()),
            expires_at=_human_dt(_load_tokens() or {}) if _load_tokens() else "-",
            flow=flow,
            message="Device Code Flow gestartet. Öffne den Link und bestätige in Tado. Danach auf 'Token abrufen (poll)'.",
            tokens_path=TOKENS_PATH,
        )
    except Exception as e:
        log.exception("auth_start failed")
        return render_template_string(
            PAGE,
            title=APP_NAME,
            tokens=_load_tokens(),
            token_valid=False if not _load_tokens() else _token_is_valid(_load_tokens()),
            expires_at=_human_dt(_load_tokens() or {}) if _load_tokens() else "-",
            flow=None,
            message=f"Fehler beim Starten des Device Flows: {e}",
            tokens_path=TOKENS_PATH,
        ), 500


@app.post("/auth/poll")
def auth_poll():
    try:
        res = _poll_device_code_for_token()
        msg = json.dumps(res, indent=2, ensure_ascii=False)
        tokens = _load_tokens()
        return render_template_string(
            PAGE,
            title=APP_NAME,
            tokens=tokens,
            token_valid=bool(tokens and _token_is_valid(tokens)),
            expires_at=_human_dt(tokens) if tokens else "-",
            flow=None,
            message=msg,
            tokens_path=TOKENS_PATH,
        )
    except Exception as e:
        log.exception("auth_poll failed")
        return render_template_string(
            PAGE,
            title=APP_NAME,
            tokens=_load_tokens(),
            token_valid=False if not _load_tokens() else _token_is_valid(_load_tokens()),
            expires_at=_human_dt(_load_tokens() or {}) if _load_tokens() else "-",
            flow=None,
            message=f"Fehler beim Polling: {e}",
            tokens_path=TOKENS_PATH,
        ), 500


@app.post("/logout")
def logout():
    _delete_tokens()
    _device_flow_state.clear()
    return redirect(url_for("index"))


# ---- WICHTIG: fehlendes Ende / Server-Start ----
if __name__ == "__main__":
    log.info("Starting %s on 0.0.0.0:%s", APP_NAME, PORT)
    # Ingress proxied → muss 0.0.0.0 sein
    app.run(host="0.0.0.0", port=PORT)
