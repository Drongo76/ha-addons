import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

import requests

try:
    import paho.mqtt.client as mqtt
except Exception:
    mqtt = None  # MQTT optional


# ===== Paths (Add-on isoliert / konfliktfrei) =====
DATA_DIR = "/data"
TOKENS_PATH = os.path.join(DATA_DIR, "tado_assistant_tokens.json")
LAST_TOKEN_RESP_PATH = os.path.join(DATA_DIR, "tado_assistant_last_token_response.json")
OPTIONS_PATH = os.path.join(DATA_DIR, "options.json")

# ===== Tado OAuth + API =====
TADO_CLIENT_ID = "1bb50063-6b0c-4d11-bd99-387f4a91cc46"
TOKEN_URL = "https://login.tado.com/oauth2/token"
API_BASE = "https://my.tado.com/api/v2"  # community spec beschreibt /api/v2 + Start mit /me :contentReference[oaicite:2]{index=2}

# ===== Defaults =====
DEFAULT_POLL_SECONDS = 60
TOKEN_REFRESH_SAFETY_SECONDS = 90  # refresh etwas vor Ablauf
HTTP_TIMEOUT = 20


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def log(msg: str) -> None:
    print(f"[worker] {msg}", flush=True)


def read_json(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except Exception as e:
        log(f"WARN: cannot read json {path}: {e}")
        return None


def write_json_atomic(path: str, obj: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def load_options() -> Dict[str, Any]:
    opt = read_json(OPTIONS_PATH) or {}
    # minimale Defaults
    opt.setdefault("poll_interval", DEFAULT_POLL_SECONDS)
    opt.setdefault("mqtt", {})  # mqtt: {enabled, host, port, username, password, topic_prefix, tls}
    opt.setdefault("topic_prefix", "tado_assistant")
    return opt


def tokens_exist() -> bool:
    return os.path.exists(TOKENS_PATH)


def parse_obtained_at(tokens: Dict[str, Any]) -> Optional[int]:
    """
    Tokens kommen bei uns mit _obtained_at ISO. Falls nicht, versuchen wir epoch-Felder.
    """
    if "_obtained_at_epoch" in tokens:
        try:
            return int(tokens["_obtained_at_epoch"])
        except Exception:
            pass
    s = tokens.get("_obtained_at")
    if not s:
        return None
    try:
        # ISO parse ohne externe libs: grob, reicht hier.
        # Beispiel: 2025-12-27T00:29:12.123456+00:00
        dt = datetime.fromisoformat(s)
        return int(dt.timestamp())
    except Exception:
        return None


def token_expires_at(tokens: Dict[str, Any]) -> Optional[int]:
    obtained = parse_obtained_at(tokens)
    expires_in = tokens.get("expires_in")
    try:
        expires_in = int(expires_in)
    except Exception:
        expires_in = None
    if obtained is None or expires_in is None:
        return None
    return obtained + expires_in


def token_needs_refresh(tokens: Dict[str, Any]) -> bool:
    exp = token_expires_at(tokens)
    if exp is None:
        # wenn wir es nicht wissen: lieber refresh versuchen (aber nur wenn refresh_token da ist)
        return True
    return int(time.time()) >= (exp - TOKEN_REFRESH_SAFETY_SECONDS)


def refresh_tokens(tokens: Dict[str, Any]) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
    """
    Refresh über OAuth2 refresh_token Grant. (Tado Device Code Flow Tokens sind refreshbar) :contentReference[oaicite:3]{index=3}
    """
    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        return False, "missing refresh_token", None

    payload = {
        "client_id": TADO_CLIENT_ID,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }

    try:
        r = requests.post(TOKEN_URL, data=payload, timeout=HTTP_TIMEOUT)
        data = r.json()
    except Exception as e:
        return False, f"refresh request failed: {e}", None

    # immer speichern fürs Debugging
    write_json_atomic(LAST_TOKEN_RESP_PATH, {"_at": now_iso(), "http_status": r.status_code, "response": data})

    if isinstance(data, dict) and data.get("access_token"):
        new_tokens = dict(tokens)
        new_tokens.update(data)
        new_tokens["_obtained_at"] = now_iso()
        new_tokens["_obtained_at_epoch"] = int(time.time())
        # Manche Provider liefern refresh_token nicht jedes Mal; wenn fehlt -> altes behalten
        if not new_tokens.get("refresh_token"):
            new_tokens["refresh_token"] = refresh_token
        return True, "ok", new_tokens

    err = ""
    if isinstance(data, dict):
        err = data.get("error") or data.get("error_description") or ""
    return False, f"refresh failed: {err or data}", None


class MqttPub:
    def __init__(self, cfg: Dict[str, Any]) -> None:
        self.cfg = cfg
        self.client = None
        self.enabled = False

    def start(self) -> None:
        mcfg = self.cfg.get("mqtt") or {}
        self.enabled = bool(mcfg.get("enabled"))
        if not self.enabled:
            log("MQTT disabled (options.json mqtt.enabled=false)")
            return
        if mqtt is None:
            log("ERROR: paho-mqtt not installed, but mqtt.enabled=true")
            return

        host = mcfg.get("host") or "core-mosquitto"
        port = int(mcfg.get("port") or 1883)
        username = mcfg.get("username")
        password = mcfg.get("password")
        tls = bool(mcfg.get("tls", False))

        client_id = mcfg.get("client_id") or f"tado_assistant_{int(time.time())}"
        c = mqtt.Client(client_id=client_id, clean_session=True)

        if username:
            c.username_pw_set(username, password=password)

        if tls:
            # default TLS settings
            c.tls_set()

        # simple log callbacks
        def on_connect(client, userdata, flags, rc):
            log(f"MQTT connected rc={rc}")

        def on_disconnect(client, userdata, rc):
            log(f"MQTT disconnected rc={rc}")

        c.on_connect = on_connect
        c.on_disconnect = on_disconnect

        log(f"MQTT connecting to {host}:{port} (tls={tls}, user={'yes' if username else 'no'})")
        c.connect(host, port, keepalive=30)
        c.loop_start()
        self.client = c

    def publish_json(self, topic: str, payload: Dict[str, Any], retain: bool = True) -> None:
        if not self.client:
            return
        try:
            self.client.publish(topic, json.dumps(payload, ensure_ascii=False), qos=0, retain=retain)
        except Exception as e:
            log(f"MQTT publish error: {e}")

    def publish_str(self, topic: str, payload: str, retain: bool = True) -> None:
        if not self.client:
            return
        try:
            self.client.publish(topic, payload, qos=0, retain=retain)
        except Exception as e:
            log(f"MQTT publish error: {e}")


def api_request(method: str, path: str, access_token: str, params: Optional[dict] = None) -> Tuple[int, Any]:
    url = f"{API_BASE}{path}"
    headers = {"Authorization": f"Bearer {access_token}"}
    r = requests.request(method, url, headers=headers, params=params, timeout=HTTP_TIMEOUT)
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, r.text


def ensure_valid_tokens() -> Dict[str, Any]:
    """
    Lädt Tokens. Wenn abgelaufen/unsicher: refresh.
    """
    tokens = read_json(TOKENS_PATH)
    if not isinstance(tokens, dict):
        raise RuntimeError(f"Tokens missing or invalid at {TOKENS_PATH}")

    if token_needs_refresh(tokens):
        ok, msg, new_tokens = refresh_tokens(tokens)
        if ok and new_tokens:
            write_json_atomic(TOKENS_PATH, new_tokens)
            log("Token refreshed.")
            return new_tokens
        else:
            raise RuntimeError(f"Token refresh failed: {msg}")

    return tokens


def get_home_ids(access_token: str) -> list:
    """
    Startpunkt: GET /me liefert u.a. homes / homeId (laut Community Spec). :contentReference[oaicite:4]{index=4}
    """
    status, data = api_request("GET", "/me", access_token)
    if status != 200:
        raise RuntimeError(f"/me failed status={status} data={data}")

    home_ids = []
    if isinstance(data, dict):
        # unterschiedliche Formen existieren in der Wildnis:
        # - "homes": [{"id": 123}, ...]
        # - "homeId": 123
        if isinstance(data.get("homes"), list):
            for h in data["homes"]:
                hid = h.get("id") or h.get("homeId")
                if isinstance(hid, int):
                    home_ids.append(hid)
        if isinstance(data.get("homeId"), int):
            home_ids.append(data["homeId"])

    home_ids = sorted(list(set(home_ids)))
    if not home_ids:
        raise RuntimeError(f"No home ids found in /me response: {data}")
    return home_ids


def get_mobile_devices(access_token: str, home_id: int) -> list:
    """
    Mobile Devices für Presence.
    Viele Clients nutzen /homes/{homeId}/mobileDevices (unofficial, aber breit verwendet).
    """
    status, data = api_request("GET", f"/homes/{home_id}/mobileDevices", access_token)
    if status != 200:
        raise RuntimeError(f"/homes/{home_id}/mobileDevices failed status={status} data={data}")
    return data if isinstance(data, list) else []


def normalize_presence(device: Dict[str, Any]) -> Dict[str, Any]:
    """
    Wir versuchen 'atHome' oder ähnliche Felder robust zu finden.
    """
    name = device.get("name") or device.get("deviceName") or device.get("model") or "unknown"
    dev_id = device.get("id") or device.get("deviceId") or None

    at_home = None
    # häufige Kandidaten:
    # - device["location"]["atHome"]
    # - device["atHome"]
    # - device["geoTrackingEnabled"] + lastKnownLocation
    if isinstance(device.get("location"), dict) and "atHome" in device["location"]:
        at_home = device["location"].get("atHome")
    elif "atHome" in device:
        at_home = device.get("atHome")

    # boolean erzwingen wenn möglich
    if isinstance(at_home, bool):
        state = "home" if at_home else "away"
    else:
        state = "unknown"

    return {
        "id": dev_id,
        "name": name,
        "state": state,
        "at_home": at_home,
        "raw": device,
        "_ts": now_iso(),
    }


def main() -> None:
    cfg = load_options()
    poll = int(cfg.get("poll_interval") or DEFAULT_POLL_SECONDS)
    topic_prefix = (cfg.get("topic_prefix") or "tado_assistant").strip().strip("/")

    log(f"starting. poll_interval={poll}s tokens={TOKENS_PATH}")
    log(f"API base: {API_BASE}")

    mpub = MqttPub(cfg)
    mpub.start()

    while True:
        try:
            if not tokens_exist():
                log(f"waiting for tokens: {TOKENS_PATH} (login first)")
                time.sleep(5)
                continue

            tokens = ensure_valid_tokens()
            access_token = tokens["access_token"]

            # home ids
            home_ids = get_home_ids(access_token)

            for home_id in home_ids:
                # mobile devices presence
                devices = get_mobile_devices(access_token, home_id)
                normalized = [normalize_presence(d) for d in devices]

                # publish (optional)
                # - aggregated
                agg_topic = f"{topic_prefix}/presence/home_{home_id}"
                payload = {
                    "home_id": home_id,
                    "devices": normalized,
                    "_ts": now_iso(),
                }
                mpub.publish_json(agg_topic, payload, retain=True)

                # - per device
                for d in normalized:
                    did = d.get("id")
                    if did is None:
                        continue
                    base = f"{topic_prefix}/presence/home_{home_id}/device_{did}"
                    mpub.publish_json(base + "/json", d, retain=True)
                    mpub.publish_str(base + "/state", d["state"], retain=True)

                log(f"presence updated home={home_id} devices={len(normalized)}")

        except Exception as e:
            # Wenn 401: einmal refresh+retry beim nächsten Loop (ensure_valid_tokens macht refresh)
            log(f"ERROR: {e}")

        time.sleep(poll)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("stopped by user")
        sys.exit(0)
    except Exception as e:
        log(f"FATAL: {e}")
        sys.exit(1)
