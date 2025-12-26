import json
import os
import time
from datetime import datetime, timezone
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse

import requests
from flask import Flask, redirect, request, make_response

app = Flask(__name__)

# tado Device Code Flow (offiziell)
TADO_CLIENT_ID = "1bb50063-6b0c-4d11-bd99-387f4a91cc46"
DEVICE_AUTHORIZE_URL = "https://login.tado.com/oauth2/device_authorize"
TOKEN_URL = "https://login.tado.com/oauth2/token"

# ✅ KONFLIKTFREI: eigene Dateinamen für dieses Add-on
DATA_DIR = "/data"
FLOW_FILE = os.path.join(DATA_DIR, "tado_assistant_flow.json")
TOKEN_FILE = os.path.join(DATA_DIR, "tado_assistant_tokens.json")
LAST_TOKEN_RESP = os.path.join(DATA_DIR, "tado_assistant_last_token_response.json")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_json(path: str):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except Exception as e:
        print(f"[WARN] _read_json failed for {path}: {e}")
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


def _file_info(path: str):
    try:
        st = os.stat(path)
        return {"exists": True, "size": st.st_size, "mtime": int(st.st_mtime)}
    except FileNotFoundError:
        return {"exists": False}
    except Exception as e:
        return {"exists": False, "error": str(e)}


def _back():
    # Ingress-sicher zurück zur vorherigen Seite
    return request.headers.get("Referer") or "../../"


def _add_query_params(url: str, extra: dict) -> str:
    p = urlparse(url)
    q = dict(parse_qsl(p.query, keep_blank_values=True))
    for k, v in extra.items():
        if v is None:
            continue
        q[k] = v
    new_query = urlencode(q)
    return urlunparse((p.scheme, p.netloc, p.path, p.params, new_query, p.fragment))


def _make_login_link(flow: dict) -> str:
    base = flow.get("verification_uri_complete") or flow.get("verification_uri") or "https://login.tado.com/oauth2/device"
    user_code = flow.get("user_code", "")
    # client_id anhängen -> vermeidet "missing_client_id"
    return _add_query_params(base, {"user_code": user_code, "client_id": TADO_CLIENT_ID})


def _flow_seconds_left(flow: dict) -> int:
    created = int(flow.get("_created_at_epoch", 0))
    expires = int(flow.get("expires_in", 0))
    if not created or not expires:
        return 0
    left = (created + expires) - int(time.time())
    return max(0, left)


def _html_page(title: str, body: str):
    html = f"""<!doctype html>
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
    .error {{ color: #b00020; font-weight: 700; }}
    .ok {{ color: #0a7a2f; font-weight: 700; }}
  </style>
</head>
<body>
  <div class="card">
    <h2>{title}</h2>
    {body}
  </div>
</body>
</html>"""
    resp = make_response(html)
    # kein Cache -> verhindert falsche "Eingeloggt"-Anzeige
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@app.get("/debug")
def debug():
    flow = _read_json(FLOW_FILE)
    tokens = _read_json(TOKEN_FILE)
    last = _read_json(LAST_TOKEN_RESP)

    info = {
        "flow_file": _file_info(FLOW_FILE),
        "token_file": _file_info(TOKEN_FILE),
        "last_token_response_file": _file_info(LAST_TOKEN_RESP),
        "flow_keys": sorted(list(flow.keys())) if isinstance(flow, dict) else None,
        "token_keys": sorted(list(tokens.keys())) if isinstance(tokens, dict) else None,
        "flow_seconds_left": _flow_seconds_left(flow) if isinstance(flow, dict) else None,
        "last_token_response": last,
    }
    resp = make_response(json.dumps(info, indent=2, ensure_ascii=False))
    resp.headers["Content-Type"] = "application/json; charset=utf-8"
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return resp


@app.get("/")
def index():
    tokens = _read_json(TOKEN_FILE)
    flow = _read_json(FLOW_FILE)

    if isinstance(tokens, dict) and tokens.get("refresh_token"):
        body = f"""
        <p class="ok">✅ Eingeloggt (Add-on Tokens)</p>
        <p class="muted">Gespeichert in <code>{TOKEN_FILE}</code></p>
        <div class="row">
          <form method="post" action="auth/logout">
            <button type="submit" class="secondary">Logout (Tokens löschen)</button>
          </form>
        </div>
        <div class="row">
          <a class="button secondary" href="debug" target="_blank" rel="noreferrer">Debug anzeigen</a>
        </div>
        """
        return _html_page("Tado Assistant (Ingress)", body)

    if isinstance(flow, dict) and flow.get("device_code"):
        left = _flow_seconds_left(flow)
        link = _make_login_link(flow)
        code = flow.get("user_code", "")
        body = f"""
        <p>🔐 Login läuft (Add-on Session). Öffne den Link und bestätige.</p>
        <p class="muted">Gültig noch: <b>{left}</b> Sekunden</p>
        <p><a class="button" href="{link}" target="_blank" rel="noreferrer">Tado Login öffnen</a></p>
        <p>Code: <span class="mono"><b>{code}</b></span></p>

        <div class="row">
          <form method="post" action="auth/poll">
            <button type="submit">Ich habe bestätigt → Token holen</button>
          </form>
        </div>
        <div class="row">
          <form method="post" action="auth/start">
            <button type="submit" class="secondary">Neuen Code erzeugen</button>
          </form>
        </div>
        <div class="row">
          <form method="post" action="auth/reset">
            <button type="submit" class="secondary">Flow zurücksetzen</button>
          </form>
        </div>
        <div class="row">
          <a class="button secondary" href="debug" target="_blank" rel="noreferrer">Debug anzeigen</a>
        </div>
        """
        return _html_page("Tado Assistant (Ingress)", body)

    if _file_info(FLOW_FILE).get("exists") and not isinstance(flow, dict):
        body = f"""
        <p class="error">⚠️ Flow-Datei existiert, ist aber nicht lesbar (leer/kaputt).</p>
        <p class="muted">{FLOW_FILE}</p>
        <div class="row">
          <form method="post" action="auth/reset">
            <button type="submit">Flow zurücksetzen</button>
          </form>
        </div>
        <div class="row">
          <a class="button secondary" href="debug" target="_blank" rel="noreferrer">Debug anzeigen</a>
        </div>
        """
        return _html_page("Tado Assistant (Ingress)", body)

    body = f"""
    <p>Starte den offiziellen tado Device-Code Login (nur für dieses Add-on).</p>
    <p class="muted">Dateien: <code>{FLOW_FILE}</code> / <code>{TOKEN_FILE}</code></p>
    <div class="row">
      <form method="post" action="auth/start">
        <button type="submit">Tado Login starten</button>
      </form>
    </div>
    <div class="row">
      <a class="button secondary" href="debug" target="_blank" rel="noreferrer">Debug anzeigen</a>
    </div>
    """
    return _html_page("Tado Assistant (Ingress)", body)


@app.post("/auth/start")
def auth_start():
    _delete_file(FLOW_FILE)
    _delete_file(LAST_TOKEN_RESP)

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
    data["_created_at_epoch"] = int(time.time())
    _write_json(FLOW_FILE, data)

    print("[INFO] device_authorize OK, flow written:", FLOW_FILE)
    return redirect(_back(), code=303)


@app.post("/auth/poll")
def auth_poll():
    flow = _read_json(FLOW_FILE)
    if not isinstance(flow, dict) or "device_code" not in flow:
        return redirect(_back(), code=303)

    device_code = flow["device_code"]

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

    _write_json(LAST_TOKEN_RESP, {"_at": _now_iso(), "http_status": r.status_code, "response": data})

    if isinstance(data, dict) and data.get("access_token"):
        tokens = {**data, "_obtained_at": _now_iso()}
        _write_json(TOKEN_FILE, tokens)
        _delete_file(FLOW_FILE)
        print("[INFO] token OK, written:", TOKEN_FILE)
        return redirect(_back(), code=303)

    err = ""
    if isinstance(data, dict):
        err = data.get("error", "") or data.get("error_description", "")
    err = err or str(data)

    body = f"""
    <p class="error">Noch kein Token: <span class="mono">{err}</span></p>
    <p class="muted">Debug liegt in <code>{LAST_TOKEN_RESP}</code></p>
    <div class="row">
      <form method="post" action="">
        <button type="submit">Nochmal Token holen</button>
      </form>
    </div>
    <div class="row">
      <a class="button secondary" href="../../">Zurück</a>
      <a class="button secondary" href="../../debug" target="_blank" rel="noreferrer">Debug anzeigen</a>
    </div>
    """
    return _html_page("Tado Login Status", body)


@app.post("/auth/reset")
def auth_reset():
    _delete_file(FLOW_FILE)
    _delete_file(LAST_TOKEN_RESP)
    return redirect(_back(), code=303)


@app.post("/auth/logout")
def auth_logout():
    _delete_file(TOKEN_FILE)
    _delete_file(FLOW_FILE)
    _delete_file(LAST_TOKEN_RESP)
    return redirect(_back(), code=303)
