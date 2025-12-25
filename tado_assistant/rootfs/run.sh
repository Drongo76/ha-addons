#!/usr/bin/env bash
set -euo pipefail

mkdir -p /data/tado_assistant

# -------------------------------------------------------------------
# Flask Web UI (Ingress) + Background Worker
# -------------------------------------------------------------------

cat >/app.py <<'PY'
import json
import os
import time
import requests
from flask import Flask, request, redirect, url_for

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
    except FileNotFoundError:
        return None
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


def refresh_access_token(refresh_token: str):
    # tado uses refresh token rotation → always store the new refresh_token
    r = requests.post(
        TOKEN_URL,
        params=dict(
            client_id=TADO_CLIENT_ID,
            grant_type="refresh_token",
            refresh_token=refresh_token,
        ),
        timeout=25,
    )
    r.raise_for_status()
    return r.json()


def get_home_id(access_token: str):
    r = requests.get(
        ME_URL,
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=25,
    )
    r.raise_for_status()
    data = r.json()
    homes = data.get("homes") or []
    if not homes:
        raise RuntimeError("Keine homes in /api/v2/me gefunden.")
    return homes[0]["id"]


def page(title: str, body_html: str):
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
    .btn:active {{ transform: translateY(1px); }}
    input {{ padding: 10px 12px; border-radius: 12px; border:1px solid #2f2f36; background:#0f0f12; color:#fff; width: 100%; max-width: 420px; }}
    code {{ background:#0f0f12; padding: 2px 6px; border-radius: 8px; border:1px solid #2a2a30; }}
    .muted {{ color:#b1b1bb; }}
    .ok {{ color:#5eead4; }}
    .warn {{ color:#fbbf24; }}
    a {{ color:#93c5fd; }}
  </style>
</head>
<body>
  <div class="wrap">
    <div style="margin-bottom: 14px;">
      <img src="/static/tado.svg" alt="tado" style="height:70px; display:block; margin-bottom:10px;" />
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
        Du startest hier den Login, bekommst einen Link + Code, bestätigst im Browser bei tado,
        dann holst du hier das Token ab.
      </p>

      <form method="post" action="/auth/start">
        <div class="row">
          <div style="flex:1; min-width:260px;">
            <input name="email_label" placeholder="Konto-Label (z.B. juergen@...)" />
          </div>
          <button class="btn primary" type="submit">Login starten</button>
        </div>
      </form>

      <div style="margin-top:16px;">
        <a class="btn" href="/auth">Login-Status öffnen</a>
      </div>

      <hr style="border:none; border-top:1px solid #26262a; margin:18px 0;" />

      <h3 style="margin:0 0 8px 0;">MQTT Entities in Home Assistant</h3>
      <p class="muted" style="margin-top:0;">
        Wenn MQTT im Add-on aktiviert ist, legt der Worker automatisch Entities an:
        pro tado Mobile Device <code>binary_sensor</code> (zuhause/weg) + ein Gesamt-Sensor (Anzahl zuhause).
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
    return redirect(url_for("auth_page"))


@app.get("/auth")
def auth_page():
    p = load_pending()
    ok, home_id, email_label = auth_status()

    if not p.get("device_code"):
        body = """
          <h2 style="margin:0 0 10px 0;">Login-Status</h2>
          <p class="muted">Noch kein Login gestartet.</p>
          <a class="btn" href="/">Zurück</a>
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

      <div style="margin-bottom:14px;">
        <div class="muted">Restzeit: <code>{remaining}</code> Sekunden</div>
      </div>

      <div class="card" style="padding:14px; border-radius:14px;">
        <div style="margin-bottom:8px;">1) Link öffnen und bei tado bestätigen:</div>
        <div style="margin-bottom:10px;"><a href="{vurl}" target="_blank" rel="noreferrer">{vurl}</a></div>
        <div>2) Falls nötig Code eingeben: <code style="font-size:18px;">{ucode}</code></div>
      </div>

      <form method="post" action="/auth/poll" style="margin-top:14px;">
        <button class="btn primary" type="submit">Token abrufen</button>
        <a class="btn" href="/">Zurück</a>
      </form>

      <p class="muted" style="margin-top:14px;">
        Hinweis: Bitte nicht schneller als alle <code>{interval}</code> Sekunden drücken.
      </p>

      <hr style="border:none; border-top:1px solid #26262a; margin:18px 0;" />
      <div class="muted">Aktueller Status: Auth={'OK' if ok else 'nicht eingerichtet'} / Home ID={home_id or '—'} / Konto-Label={email_label or '—'}</div>
    """
    return page("Login", body)


@app.post("/auth/poll")
def auth_poll():
    p = load_pending()
    device_code = p.get("device_code")
    if not device_code:
        return redirect(url_for("auth_page"))

    r = requests.post(
        TOKEN_URL,
        params=dict(
            client_id=TADO_CLIENT_ID,
            device_code=device_code,
            grant_type="urn:ietf:params:oauth:grant-type:device_code",
        ),
        timeout=25,
    )

    # If not authorized yet, tado returns an error JSON → show a short hint
    if r.status_code != 200:
        body = f"""
          <h2 style="margin:0 0 10px 0;">Noch nicht bestätigt</h2>
          <p class="muted">Bei tado noch nicht final bestätigt oder zu früh gepollt.</p>
          <pre style="white-space:pre-wrap; background:#0f0f12; padding:12px; border-radius:12px; border:1px solid #2a2a30;">{r.text}</pre>
          <a class="btn primary" href="/auth">Zurück</a>
        """
        return page("Warten", body)

    token = r.json()
    refresh_token = token.get("refresh_token")
    access_token = token.get("access_token")
    if not refresh_token or not access_token:
        body = """
          <h2 style="margin:0 0 10px 0;">Fehler</h2>
          <p class="muted">Token-Antwort war unvollständig.</p>
          <a class="btn" href="/auth">Zurück</a>
        """
        return page("Fehler", body)

    home_id = get_home_id(access_token)

    auth = {
        "email_label": p.get("email_label", ""),
        "home_id": home_id,
        "refresh_token": refresh_token,
        "saved": int(time.time()),
    }
    _write_json(AUTH_FILE, auth)

    # clear pending
    try:
        os.remove(PENDING_FILE)
    except FileNotFoundError:
        pass

    return redirect(url_for("index"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8099)
PY


cat >/worker.py <<'PY'
import json
import os
import time
import threading
import requests

try:
    import paho.mqtt.client as mqtt
except Exception:
    mqtt = None

APP_DIR = "/data/tado_assistant"
AUTH_FILE = os.path.join(APP_DIR, "auth.json")
STATE_FILE = os.path.join(APP_DIR, "state.json")

TADO_CLIENT_ID = "1bb50063-6b0c-4d11-bd99-387f4a91cc46"
TOKEN_URL = "https://login.tado.com/oauth2/token"
ME_URL = "https://my.tado.com/api/v2/me"
PRESENCE_URL = "https://my.tado.com/api/v2/homes/{home_id}/presence"
PRESENCELOCK_URL = "https://my.tado.com/api/v2/homes/{home_id}/presenceLock"


def read_json(path: str):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except Exception:
        return None


def write_json(path: str, data: dict):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def load_options():
    # Home Assistant add-on options live here
    opt = read_json("/data/options.json") or {}
    return opt


def refresh_access_token(refresh_token: str):
    r = requests.post(
        TOKEN_URL,
        params=dict(
            client_id=TADO_CLIENT_ID,
            grant_type="refresh_token",
            refresh_token=refresh_token,
        ),
        timeout=25,
    )
    r.raise_for_status()
    return r.json()


def api_get_me(access_token: str):
    r = requests.get(
        ME_URL,
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=25,
    )
    r.raise_for_status()
    return r.json()


def api_set_presence(access_token: str, home_id: int, home_presence: str):
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json;charset=utf-8",
    }
    body = {"homePresence": home_presence}

    # Try /presence first, then /presenceLock as fallback (some setups use it)
    for url in (PRESENCE_URL.format(home_id=home_id), PRESENCELOCK_URL.format(home_id=home_id)):
        r = requests.put(url, headers=headers, json=body, timeout=25)
        if r.status_code in (200, 204):
            return True
    return False


class MqttPub:
    def __init__(self, host, port, username, password, discovery_prefix, base_topic):
        self.host = host
        self.port = int(port)
        self.username = username or None
        self.password = password or None
        self.discovery_prefix = discovery_prefix.strip("/") or "homeassistant"
        self.base_topic = base_topic.strip("/") or "tado_assistant"

        self.client = mqtt.Client()
        if self.username is not None and self.username != "":
            self.client.username_pw_set(self.username, self.password or "")

        self.connected = False
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect

    def _on_connect(self, client, userdata, flags, rc):
        self.connected = True

    def _on_disconnect(self, client, userdata, rc):
        self.connected = False

    def connect(self):
        self.client.connect(self.host, self.port, keepalive=60)
        self.client.loop_start()

    def publish(self, topic, payload, retain=True):
        if not self.connected:
            return
        if isinstance(payload, (dict, list)):
            payload = json.dumps(payload, ensure_ascii=False)
        else:
            payload = str(payload)
        self.client.publish(topic, payload, retain=retain)

    def discovery_topic(self, component, object_id):
        return f"{self.discovery_prefix}/{component}/{object_id}/config"


def slugify(s: str):
    out = []
    for ch in s.lower():
        if ch.isalnum():
            out.append(ch)
        else:
            out.append("_")
    return "_".join("".join(out).split("_"))


def worker_loop():
    mqtt_pub = None
    discovery_sent = set()

    while True:
        opt = load_options()
        poll_seconds = int(opt.get("poll_seconds", 60))
        set_presence = bool(opt.get("set_presence", True))

        mqtt_enabled = bool(opt.get("mqtt_enabled", True))
        if mqtt_enabled and mqtt is not None and mqtt_pub is None:
            mqtt_pub = MqttPub(
                host=opt.get("mqtt_host", "core-mosquitto"),
                port=opt.get("mqtt_port", 1883),
                username=opt.get("mqtt_username", ""),
                password=opt.get("mqtt_password", ""),
                discovery_prefix=opt.get("mqtt_discovery_prefix", "homeassistant"),
                base_topic=opt.get("mqtt_base_topic", "tado_assistant"),
            )
            try:
                mqtt_pub.connect()
            except Exception:
                mqtt_pub = None

        auth = read_json(AUTH_FILE) or {}
        refresh_token = auth.get("refresh_token")
        home_id = auth.get("home_id")

        if not refresh_token or not home_id:
            time.sleep(2)
            continue

        try:
            token = refresh_access_token(refresh_token)
            access_token = token.get("access_token")
            new_refresh = token.get("refresh_token")
            if new_refresh:
                auth["refresh_token"] = new_refresh
                auth["saved"] = int(time.time())
                write_json(AUTH_FILE, auth)

            if not access_token:
                time.sleep(poll_seconds)
                continue

            me = api_get_me(access_token)
            devices = me.get("mobileDevices") or []

            # Determine per-device atHome
            at_home = []
            device_states = []
            for d in devices:
                name = d.get("name") or f"device_{d.get('id')}"
                loc = (d.get("location") or {})
                settings = (d.get("settings") or {})
                if settings.get("geoTrackingEnabled") is False:
                    continue
                is_home = bool(loc.get("atHome"))
                is_stale = bool(loc.get("stale"))
                device_id = str(d.get("id") or slugify(name))
                device_states.append({
                    "id": device_id,
                    "name": name,
                    "home": is_home,
                    "stale": is_stale,
                })
                if is_home and not is_stale:
                    at_home.append(device_id)

            desired = "HOME" if len(at_home) > 0 else "AWAY"

            # Publish MQTT entities
            if mqtt_pub is not None and mqtt_pub.connected:
                # Device definition for HA device registry
                ha_device = {
                    "identifiers": [f"tado_assistant_{home_id}"],
                    "name": "Tado Assistant",
                    "manufacturer": "tado°",
                    "model": "Auto-Assist (Add-on)",
                }

                # Aggregate sensors
                obj_any = "tado_assistant_anyone_home"
                if obj_any not in discovery_sent:
                    mqtt_pub.publish(
                        mqtt_pub.discovery_topic("binary_sensor", obj_any),
                        {
                            "name": "Tado Anyone Home",
                            "unique_id": obj_any,
                            "state_topic": f"{mqtt_pub.base_topic}/anyone_home",
                            "payload_on": "ON",
                            "payload_off": "OFF",
                            "device": ha_device,
                        },
                    )
                    discovery_sent.add(obj_any)

                obj_cnt = "tado_assistant_home_count"
                if obj_cnt not in discovery_sent:
                    mqtt_pub.publish(
                        mqtt_pub.discovery_topic("sensor", obj_cnt),
                        {
                            "name": "Tado Home Count",
                            "unique_id": obj_cnt,
                            "state_topic": f"{mqtt_pub.base_topic}/home_count",
                            "device_class": None,
                            "device": ha_device,
                        },
                    )
                    discovery_sent.add(obj_cnt)

                mqtt_pub.publish(f"{mqtt_pub.base_topic}/anyone_home", "ON" if desired == "HOME" else "OFF")
                mqtt_pub.publish(f"{mqtt_pub.base_topic}/home_count", str(len(at_home)))

                # Per device presence as binary_sensor
                for st in device_states:
                    obj = f"tado_assistant_dev_{slugify(st['id'])}"
                    if obj not in discovery_sent:
                        mqtt_pub.publish(
                            mqtt_pub.discovery_topic("binary_sensor", obj),
                            {
                                "name": f"Tado {st['name']} Home",
                                "unique_id": obj,
                                "state_topic": f"{mqtt_pub.base_topic}/devices/{st['id']}/home",
                                "payload_on": "ON",
                                "payload_off": "OFF",
                                "device": ha_device,
                            },
                        )
                        discovery_sent.add(obj)

                    mqtt_pub.publish(
                        f"{mqtt_pub.base_topic}/devices/{st['id']}/home",
                        "ON" if (st["home"] and not st["stale"]) else "OFF",
                    )

            # Set presence in tado
            if set_presence:
                state = read_json(STATE_FILE) or {}
                last = state.get("last_presence")
                if last != desired:
                    ok = api_set_presence(access_token, int(home_id), desired)
                    if ok:
                        state["last_presence"] = desired
                        state["changed"] = int(time.time())
                        write_json(STATE_FILE, state)

        except Exception:
            # keep running; avoid crashing add-on
            pass

        time.sleep(poll_seconds)


if __name__ == "__main__":
    t = threading.Thread(target=worker_loop, daemon=False)
    t.start()
    t.join()
PY

# Start worker in background, keep web UI in foreground
python3 /worker.py &
exec python3 /app.py
