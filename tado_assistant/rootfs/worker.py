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


# ===== Paths =====
DATA_DIR = "/data"
TOKENS_PATH = os.path.join(DATA_DIR, "tado_assistant_tokens.json")
LAST_TOKEN_RESP_PATH = os.path.join(DATA_DIR, "tado_assistant_last_token_response.json")
OPTIONS_PATH = os.path.join(DATA_DIR, "options.json")

# ===== Tado OAuth + API =====
TADO_CLIENT_ID = "1bb50063-6b0c-4d11-bd99-387f4a91cc46"
TOKEN_URL = "https://login.tado.com/oauth2/token"
API_BASE = "https://my.tado.com/api/v2"

# ===== Defaults =====
DEFAULT_POLL_SECONDS = 60
TOKEN_REFRESH_SAFETY_SECONDS = 90
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


# ✅ WICHTIG: Unterstützt BOTH Formate:
# - alt/verschachtelt: mqtt: { enabled, host, ... } + poll_interval
# - HA-UI flach: mqtt_enabled, mqtt_host, ... + poll_seconds
def load_config() -> Dict[str, Any]:
    opt = read_json(OPTIONS_PATH) or {}

    # Poll
    poll = opt.get("poll_interval", None)
    if poll is None:
        poll = opt.get("poll_seconds", DEFAULT_POLL_SECONDS)
    try:
        poll = int(poll)
    except Exception:
        poll = DEFAULT_POLL_SECONDS

    # MQTT config (verschachtelt ODER flach)
    mcfg = opt.get("mqtt", None)
    if not isinstance(mcfg, dict):
        mcfg = {}

    # flache Keys übernehmen (falls gesetzt)
    if "mqtt_enabled" in opt:
        mcfg["enabled"] = bool(opt.get("mqtt_enabled"))
    if "mqtt_host" in opt:
        mcfg["host"] = opt.get("mqtt_host")
    if "mqtt_port" in opt:
        mcfg["port"] = opt.get("mqtt_port")
    if "mqtt_username" in opt:
        mcfg["username"] = opt.get("mqtt_username")
    if "mqtt_password" in opt:
        mcfg["password"] = opt.get("mqtt_password")
    if "mqtt_tls" in opt:
        mcfg["tls"] = bool(opt.get("mqtt_tls"))

    # Topic Prefix: bevorzugt mqtt_topic_prefix, sonst topic_prefix, sonst Default
    topic_prefix = opt.get("mqtt_topic_prefix") or opt.get("topic_prefix") or "tado_assistant"
    topic_prefix = str(topic_prefix).strip().strip("/")

    # Discovery Prefix (optional, später)
    discovery_prefix = opt.get("mqtt_discovery_prefix") or opt.get("discovery_prefix") or "homeassistant"
    discovery_prefix = str(discovery_prefix).strip().strip("/")

    return {
        "poll_seconds": poll,
        "mqtt": mcfg,
        "topic_prefix": topic_prefix,
        "discovery_prefix": discovery_prefix,
        "raw": opt,
    }


def tokens_exist() -> bool:
    return os.path.exists(TOKENS_PATH)


def parse_obtained_at(tokens: Dict[str, Any]) -> Optional[int]:
    if "_obtained_at_epoch" in tokens:
        try:
            return int(tokens["_obtained_at_epoch"])
        except Exception:
            pass
    s = tokens.get("_obtained_at")
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
        return int(dt.timestamp())
    except Exception:
        return None


def token_expires_at(tokens: Dict[str, Any]) -> Optional[int]:
    obtained = parse_obtained_at(tokens)
    try:
        expires_in = int(tokens.get("expires_in"))
    except Exception:
        expires_in = None
    if obtained is None or expires_in is None:
        return None
    return obtained + expires_in


def token_needs_refresh(tokens: Dict[str, Any]) -> bool:
    exp = token_expires_at(tokens)
    if exp is None:
        return True
    return int(time.time()) >= (exp - TOKEN_REFRESH_SAFETY_SECONDS)


def refresh_tokens(tokens: Dict[str, Any]) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
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

    write_json_atomic(LAST_TOKEN_RESP_PATH, {"_at": now_iso(), "http_status": r.status_code, "response": data})

    if isinstance(data, dict) and data.get("access_token"):
        new_tokens = dict(tokens)
        new_tokens.update(data)
        new_tokens["_obtained_at"] = now_iso()
        new_tokens["_obtained_at_epoch"] = int(time.time())
        if not new_tokens.get("refresh_token"):
            new_tokens["refresh_token"] = refresh_token
        return True, "ok", new_tokens

    err = ""
    if isinstance(data, dict):
        err = data.get("error") or data.get("error_description") or ""
    return False, f"refresh failed: {err or data}", None


class MqttPub:
    def __init__(self, mqtt_cfg: Dict[str, Any]) -> None:
        self.cfg = mqtt_cfg or {}
        self.client = None

    def start(self) -> None:
        enabled = bool(self.cfg.get("enabled", False))
        if not enabled:
            log("MQTT disabled (options.json mqtt_enabled=false / mqtt.enabled=false)")
            return
        if mqtt is None:
            log("ERROR: paho-mqtt not installed, but mqtt enabled")
            return

        host = self.cfg.get("host") or "core-mosquitto"
        port = int(self.cfg.get("port") or 1883)
        username = self.cfg.get("username")
        password = self.cfg.get("password")
        tls = bool(self.cfg.get("tls", False))

        client_id = self.cfg.get("client_id") or f"tado_assistant_{int(time.time())}"
        c = mqtt.Client(client_id=client_id, clean_session=True)

        if username:
            c.username_pw_set(username, password=password)
        if tls:
            c.tls_set()

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
    tokens = read_json(TOKENS_PATH)
    if not isinstance(tokens, dict):
        raise RuntimeError(f"Tokens missing or invalid at {TOKENS_PATH}")

    if token_needs_refresh(tokens):
        ok, msg, new_tokens = refresh_tokens(tokens)
        if ok and new_tokens:
            write_json_atomic(TOKENS_PATH, new_tokens)
            log("Token refreshed.")
            return new_tokens
        raise RuntimeError(f"Token refresh failed: {msg}")

    return tokens


def get_home_ids(access_token: str) -> list:
    status, data = api_request("GET", "/me", access_token)
    if status != 200:
        raise RuntimeError(f"/me failed status={status} data={data}")

    home_ids = []
    if isinstance(data, dict):
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
    status, data = api_request("GET", f"/homes/{home_id}/mobileDevices", access_token)
    if status != 200:
        raise RuntimeError(f"/homes/{home_id}/mobileDevices failed status={status} data={data}")
    return data if isinstance(data, list) else []


def normalize_presence(device: Dict[str, Any]) -> Dict[str, Any]:
    name = device.get("name") or device.get("deviceName") or device.get("model") or "unknown"
    dev_id = device.get("id") or device.get("deviceId")

    at_home = None
    if isinstance(device.get("location"), dict) and "atHome" in device["location"]:
        at_home = device["location"].get("atHome")
    elif "atHome" in device:
        at_home = device.get("atHome")

    if isinstance(at_home, bool):
        state = "home" if at_home else "away"
    else:
        state = "unknown"

    return {
        "id": dev_id,
        "name": name,
        "state": state,
        "at_home": at_home,
        "_ts": now_iso(),
    }


def main() -> None:
    cfg = load_config()
    poll = cfg["poll_seconds"]
    topic_prefix = cfg["topic_prefix"]

    log(f"starting. poll_seconds={poll} tokens={TOKENS_PATH}")
    log(f"options raw keys={sorted(list((cfg.get('raw') or {}).keys()))}")

    mpub = MqttPub(cfg["mqtt"])
    mpub.start()

    while True:
        try:
            if not tokens_exist():
                log(f"waiting for tokens: {TOKENS_PATH} (login first)")
                time.sleep(5)
                continue

            tokens = ensure_valid_tokens()
            access_token = tokens["access_token"]

            home_ids = get_home_ids(access_token)

            for home_id in home_ids:
                devices = get_mobile_devices(access_token, home_id)
                normalized = [normalize_presence(d) for d in devices]

                agg_topic = f"{topic_prefix}/presence/home_{home_id}"
                mpub.publish_json(
                    agg_topic,
                    {"home_id": home_id, "devices": normalized, "_ts": now_iso()},
                    retain=True,
                )

                for d in normalized:
                    did = d.get("id")
                    if not did:
                        continue
                    base = f"{topic_prefix}/presence/home_{home_id}/device_{did}"
                    mpub.publish_json(base + "/json", d, retain=True)
                    mpub.publish_str(base + "/state", d["state"], retain=True)

                log(f"presence updated home={home_id} devices={len(normalized)}")

        except Exception as e:
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
