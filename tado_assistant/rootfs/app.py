import os
import json
import time
import signal
import subprocess
import logging
from pathlib import Path

import requests
from flask import Flask, request, redirect, Response
from werkzeug.serving import run_simple

DATA_DIR = Path("/data")
DATA_DIR.mkdir(parents=True, exist_ok=True)

PID_FILE = DATA_DIR / "worker.pid"

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


def ingress_base() -> str:
    base = request.headers.get("X-Ingress-Path", "")
    if not base:
        return "/"
    if not base.endswith("/"):
        base += "/"
    return base


def load_json(path: Path, default=None):
    try:
        if not path.exists():
            return default
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path: Path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _pid_alive(pid: int) -> bool:
    if pid <= 1:
        return False
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def worker_status():
    try:
        pid_txt = PID_FILE.read_text(encoding="utf-8").strip()
        pid = int(pid_txt)
    except Exception:
        return False, None
    if _pid_alive(pid):
        return True, pid
    return False, pid


def worker_start():
    running, pid = worker_status()
    if running:
        return True, pid

    p = subprocess.Popen(
        ["python3", "/worker.py"],
        stdout=None,
        stderr=None,
        env=os.environ.copy(),
        start_new_session=True,
    )
    PID_FILE.write_text(str(p.pid), encoding="utf-8")
    return True, p.pid


def worker_stop():
    running, pid = worker_status()
    if not pid:
        try:
        # cleanup
            if PID_FILE.exists():
                PID_FILE.unlink()
        except Exception:
            pass
        return True, None

    if running:
        try:
            os.kill(pid, signal.SIGTERM)
        except Exception:
            pass

        t0 = time.time()
        while time.time() - t0 < 5:
            if not _pid_alive(pid):
                break
            time.sleep(0.2)

    try:
        if PID_FILE.exists():
            PID_FILE.unlink()
    except Exception:
        pass

    return True, pid


def html_page(body: str):
    base = ingress_base()
    css = """
    <style>
      body { font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif; margin: 20px; }
      .wrap { max-width: 900px; margin: 0 auto; }
      .top { display:flex; align-items:center; justify-content:space-between; gap:16px; margin-bottom:18px; }
      .card { border: 1px solid #ddd; border-radius: 12px; padding: 16px; margin: 14px 0; }
      .muted { color: #666; }
      button { padding: 10px 14px; border-radius: 10px; border: 1px solid #ccc; background: #f8f8f8; cursor:pointer; }
      button.primary { background: #111; color:#fff; border-color:#111; }
      .ok { color: #0a7; font-weight: 700; }
      .bad { color: #c22; font-weight: 700; }
      .row { display:flex; gap:10px; flex-wrap:wrap; align-items:center; }
      code { background:#f6f6f6; padding:2px 6px; border-radius:8px; }
    </style>
    """
    header = """
    <div class="top">
      <div>
        <h2 style="margin:0;">Tado Assistant (Ingress)</h2>
        <div class="muted">Login + Worker</div>
      </div>
      <img src="static/tado.svg" alt="tado" style="height:28px; opacity:.9"/>
    </div>
    """
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<base href='{base}'>"
        f"{css}</head><body><div class='wrap'>{header}{body}</div></body></html>"
    )


@app.get("/")
def index():
    running, pid = worker_status()
    if running:
        wline = f"<span class='ok'>✅ Automatisierung läuft</span> &nbsp; <span class='muted'>(PID {pid})</span>"
    else:
        wline = "<span class='bad'>⛔ Automatisierung aus</span>"

    body = f"""
    <div class="card">
      <h2 style="margin-top:0;">Automatisierung</h2>
      <div>{wline}</div>
      <div class="row" style="margin-top:12px;">
        <form method="post" action="automation/start"><button class="primary" type="submit">Start</button></form>
        <form method="post" action="automation/stop"><button type="submit">Stop</button></form>
      </div>
    </div>

    <div class="card">
      <h2 style="margin-top:0;">Login (später)</h2>
      <div class="muted">UI läuft. Automatisierung kann gestartet/gestoppt werden.</div>
    </div>
    """
    return Response(html_page(body), mimetype="text/html")


@app.post("/automation/start")
def automation_start():
    worker_start()
    return redirect("./", code=302)


@app.post("/automation/stop")
def automation_stop():
    worker_stop()
    return redirect("./", code=302)


@app.get("/health")
def health():
    running, pid = worker_status()
    return {"ok": True, "worker_running": running, "worker_pid": pid}


if __name__ == "__main__":
    os.environ["FLASK_ENV"] = "production"
    os.environ["FLASK_DEBUG"] = "0"
    os.environ["WERKZEUG_DEBUG_PIN"] = "off"

    run_simple(
        "0.0.0.0",
        int(os.getenv("PORT", "8099")),
        app,
        use_reloader=False,
        use_debugger=False,
        threaded=True,
    )
