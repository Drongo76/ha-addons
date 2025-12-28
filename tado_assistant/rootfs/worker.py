import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple, List, Set, Callable

import requests

try:
    import paho.mqtt.client as mqtt
except Exception:
    mqtt = None  # type: ignore


# -----------------------------
# Paths / constants
# -----------------------------
DATA_DIR = "/data"
OPTIONS_PATH = "/data/options.json"

TOKENS_PATH = os.path.join(DATA_DIR, "tado_assistant_tokens.json")
DISCOVERY_STATE_PATH = os.path.join(DATA_DIR, "tado_assistant_discovery_state.json")
LAST_DEVICES_PATH = os.path.join(DATA_DIR, "tado_assistant_last_devices.json")

# Auto-Assist runtime (enabled is forced OFF on every boot)
AUTO_ASSIST_STATE_PATH = os.path.join(DATA_DIR, "tado_assistant_auto_assist_state.json")

API_BASE = "https://my.tado.com/api/v2"

# Public OAuth refresh endpoint (NO client_secret needed)
TOKEN_URL = "https://login.tado.com/oauth2/token"
TADO_CLIENT_ID = "1bb50063-6b0c-4d11-bd99-387f4a91cc46"

HTTP_TIMEOUT = 20
DEFAULT_POLL_SECONDS = 300

DISCOVERY_REPUBLISH_EVERY_LOOPS = 20

DEFAULT_RATE_LIMIT_BACKOFF_SECONDS = 120
DEFAULT_RATE_LIMIT_BACKOFF_MAX_SECONDS = 1800


# Auto-Assist MQTT topics (relative to topic_prefix)
AUTO_ASSIST_STATE_TOPIC = "auto_assist/state"   # payload: ON/OFF
AUTO_ASSIST_SET_TOPIC = "auto_assist/set"       # payload: ON/OFF
AUTO_ASSIST_ATTRS_TOPIC = "auto_assist/attrs"   # JSON: last_run/last_action/last_error


# -----------------------------
# Small helpers
# -----------------------------
def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def log(msg: str) -> None:
    print(f"[worker] {msg}", flush=True)


def read_json(path: str) -> Optional[Any]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except Exception as e:
        log(f"WARN: cannot read json {path}: {e}")
        return None


def write_json_atomic(path: str, obj: Any) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def tokens_exist() -> bool:
    return os.path.exists(TOKENS_PATH)


def _parse_int_list(val: Any) -> List[int]:
    out: List[int] = []
    if val is None:
        return out
    if isinstance(val, int):
        return [val]
    if isinstance(val, str):
        parts = [p.strip() for p in val.split(",") if p.strip()]
        for p in parts:
            try:
                out.append(int(p))
            except Exception:
                pass
        return out
    if isinstance(val, list):
        for x in val:
            try:
                out.append(int(x))
            except Exception:
                continue
        return out
    return out


# -----------------------------
# Config
# -----------------------------
def load_config() -> Dict[str, Any]:
    opt = read_json(OPTIONS_PATH) or {}

    poll = opt.get("poll_seconds", opt.get("poll_interval", DEFAULT_POLL_SECONDS))
    try:
        poll = int(poll)
    except Exception:
        poll = DEFAULT_POLL_SECONDS
    poll = max(10, poll)

    enable_raw_sensors = opt.get("enable_raw_sensors", True)
    enable_raw_sensors = bool(enable_raw_sensors)

    tado_home_ids = opt.get("tado_home_ids", opt.get("home_ids"))
    if isinstance(tado_home_ids, str):
        parts = [p.strip() for p in tado_home_ids.split(",") if p.strip()]
        try:
            tado_home_ids = [int(p) for p in parts]
        except Exception:
            tado_home_ids = None
    if isinstance(tado_home_ids, list):
        tado_home_ids = sorted(list(set(_parse_int_list(tado_home_ids))))
    else:
        tado_home_ids = None

    rate_backoff = opt.get("rate_limit_backoff_seconds", DEFAULT_RATE_LIMIT_BACKOFF_SECONDS)
    rate_backoff_max = opt.get("rate_limit_backoff_max_seconds", DEFAULT_RATE_LIMIT_BACKOFF_MAX_SECONDS)
    try:
        rate_backoff = int(rate_backoff)
    except Exception:
        rate_backoff = DEFAULT_RATE_LIMIT_BACKOFF_SECONDS
    try:
        rate_backoff_max = int(rate_backoff_max)
    except Exception:
        rate_backoff_max = DEFAULT_RATE_LIMIT_BACKOFF_MAX_SECONDS
    rate_backoff = max(5, rate_backoff)
    rate_backoff_max = max(rate_backoff, rate_backoff_max)

    mcfg = opt.get("mqtt", {})
    if not isinstance(mcfg, dict):
        mcfg = {}

    # flat keys
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
    if "mqtt_client_id" in opt:
        mcfg["client_id"] = opt.get("mqtt_client_id")

    topic_prefix = opt.get("mqtt_topic_prefix") or opt.get("topic_prefix") or "tado_assistant"
    topic_prefix = str(topic_prefix).strip().strip("/")

    discovery_prefix = opt.get("mqtt_discovery_prefix") or opt.get("discovery_prefix") or "homeassistant"
    discovery_prefix = str(discovery_prefix).strip().strip("/")

    ha_device_name = opt.get("ha_device_name") or "Tado Assistant"
    ha_device_id = opt.get("ha_device_id") or "tado_assistant"

    return {
        "poll_seconds": poll,
        "enable_raw_sensors": enable_raw_sensors,
        "tado_home_ids": tado_home_ids,
        "rate_limit_backoff_seconds": rate_backoff,
        "rate_limit_backoff_max_seconds": rate_backoff_max,
        "mqtt": mcfg,
        "topic_prefix": topic_prefix,
        "discovery_prefix": discovery_prefix,
        "ha_device_name": ha_device_name,
        "ha_device_id": ha_device_id,
        "raw": opt,
    }


# -----------------------------
# MQTT
# -----------------------------
class MqttPub:
    def __init__(self, cfg: Dict[str, Any]):
        self.cfg = cfg or {}
        self.client = None
        self._on_message_cb: Optional[Callable[[str, str], None]] = None
        self._on_connect_cb: Optional[Callable[[], None]] = None

    def set_on_message(self, cb: Callable[[str, str], None]) -> None:
        self._on_message_cb = cb

    def set_on_connect(self, cb: Callable[[], None]) -> None:
        self._on_connect_cb = cb

    def start(self, lwt_topic: Optional[str] = None) -> None:
        if mqtt is None:
            log("WARN: paho-mqtt not available, MQTT disabled")
            return

        enabled = self.cfg.get("enabled", True)
        if not enabled:
            log("MQTT disabled in config")
            return

        host = self.cfg.get("host") or "core-mosquitto"
        port = int(self.cfg.get("port") or 1883)
        username = self.cfg.get("username")
        password = self.cfg.get("password")
        use_tls = bool(self.cfg.get("tls", False))
        client_id = self.cfg.get("client_id") or f"tado_assistant_{int(time.time())}"

        self.client = mqtt.Client(client_id=client_id, clean_session=True)

        if lwt_topic:
            try:
                self.client.will_set(lwt_topic, payload="offline", qos=0, retain=True)
            except Exception as e:
                log(f"WARN: cannot set MQTT LWT: {e}")

        if username:
            self.client.username_pw_set(str(username), str(password or ""))

        if use_tls:
            try:
                self.client.tls_set()
            except Exception as e:
                log(f"WARN: cannot enable MQTT TLS: {e}")

        def on_connect(client, userdata, flags, rc):
            log(f"MQTT connected rc={rc}")
            if self._on_connect_cb:
                try:
                    self._on_connect_cb()
                except Exception as e:
                    log(f"WARN: on_connect callback failed: {e}")

        def on_disconnect(client, userdata, rc):
            log(f"MQTT disconnected rc={rc}")

        def on_message(client, userdata, msg):
            try:
                t = msg.topic or ""
                p = msg.payload.decode("utf-8", errors="ignore") if msg.payload else ""
                if self._on_message_cb:
                    self._on_message_cb(t, p)
            except Exception:
                pass

        self.client.on_connect = on_connect
        self.client.on_disconnect = on_disconnect
        self.client.on_message = on_message

        log(f"MQTT connecting to {host}:{port} (tls={use_tls}, user={'yes' if username else 'no'})")
        self.client.connect(host, port, keepalive=60)
        self.client.loop_start()

    def subscribe(self, topic: str) -> None:
        if not self.client:
            return
        self.client.subscribe(topic)

    def publish(self, topic: str, payload: str, retain: bool = True) -> None:
        if not self.client:
            return
        self.client.publish(topic, payload, qos=0, retain=retain)

    def publish_json(self, topic: str, payload: Any, retain: bool = True) -> None:
        self.publish(topic, json.dumps(payload, ensure_ascii=False), retain=retain)

    def publish_delete_retained(self, topic: str) -> None:
        self.publish(topic, "", retain=True)


# -----------------------------
# Tado API
# -----------------------------
class RateLimitError(Exception):
    def __init__(self, path: str, retry_after: Optional[int] = None):
        super().__init__(path)
        self.path = path
        self.retry_after = retry_after


def api_request(method: str, path: str, access_token: str, params: Optional[dict] = None) -> Tuple[int, Any, Dict[str, str]]:
    url = f"{API_BASE}{path}"
    headers = {"Authorization": f"Bearer {access_token}"}
    r = requests.request(method, url, headers=headers, params=params, timeout=HTTP_TIMEOUT)
    resp_headers = {k: v for k, v in r.headers.items()}
    try:
        data = r.json()
    except Exception:
        data = r.text
    return r.status_code, data, resp_headers


def parse_retry_after(headers: Dict[str, str]) -> Optional[int]:
    ra = headers.get("Retry-After") or headers.get("retry-after")
    if not ra:
        return None
    try:
        return int(float(ra))
    except Exception:
        return None


def get_home_ids(access_token: str) -> List[int]:
    status, data, headers = api_request("GET", "/me", access_token)
    if status == 429:
        raise RateLimitError("/me", parse_retry_after(headers))
    if status != 200:
        raise RuntimeError(f"/me failed status={status} data={data}")

    home_ids: List[int] = []
    if isinstance(data, dict):
        homes = data.get("homes")
        if isinstance(homes, list):
            for h in homes:
                if isinstance(h, dict):
                    hid = h.get("id") or h.get("homeId")
                    if isinstance(hid, int):
                        home_ids.append(hid)
        if isinstance(data.get("homeId"), int):
            home_ids.append(data["homeId"])

    home_ids = sorted(list(set(home_ids)))
    if not home_ids:
        raise RuntimeError(f"No home ids found in /me response: {data}")
    return home_ids


def get_mobile_devices(access_token: str, home_id: int) -> List[Dict[str, Any]]:
    path = f"/homes/{home_id}/mobileDevices"
    status, data, headers = api_request("GET", path, access_token)
    if status == 429:
        raise RateLimitError(path, parse_retry_after(headers))
    if status != 200:
        raise RuntimeError(f"{path} failed status={status} data={data}")
    return data if isinstance(data, list) else []


# -----------------------------
# Token handling
# -----------------------------
def _obtained_epoch(tokens: Dict[str, Any]) -> Optional[int]:
    if "_obtained_at_epoch" in tokens:
        try:
            return int(tokens["_obtained_at_epoch"])
        except Exception:
            pass
    s = tokens.get("_obtained_at")
    if isinstance(s, str) and s:
        try:
            return int(datetime.fromisoformat(s).timestamp())
        except Exception:
            return None
    return None


def token_expires_at(tokens: Dict[str, Any]) -> Optional[int]:
    obtained = _obtained_epoch(tokens)
    try:
        expires_in = int(tokens.get("expires_in"))
    except Exception:
        return None
    if obtained is None:
        return None
    return obtained + expires_in


def token_is_expired(tokens: Dict[str, Any], skew_seconds: int = 60) -> bool:
    exp = token_expires_at(tokens)
    if exp is None:
        return True
    return time.time() >= (exp - skew_seconds)


def refresh_tokens(tokens: Dict[str, Any]) -> Dict[str, Any]:
    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        raise RuntimeError("refresh_token missing in tokens file (login again via Ingress)")

    payload = {
        "client_id": TADO_CLIENT_ID,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }

    r = requests.post(TOKEN_URL, data=payload, timeout=HTTP_TIMEOUT)
    if r.status_code == 429:
        ra = parse_retry_after({k: v for k, v in r.headers.items()})
        raise RateLimitError("oauth2/token", ra)

    try:
        data = r.json()
    except Exception:
        data = r.text

    if r.status_code != 200 or not (isinstance(data, dict) and data.get("access_token")):
        raise RuntimeError(f"token refresh failed status={r.status_code} data={data}")

    new_tokens = dict(tokens)
    new_tokens.update(data)

    if not new_tokens.get("refresh_token"):
        new_tokens["refresh_token"] = refresh_token

    new_tokens["_obtained_at"] = now_iso()
    new_tokens["_obtained_at_epoch"] = int(time.time())

    write_json_atomic(TOKENS_PATH, new_tokens)
    log("Token refreshed.")
    return new_tokens


def ensure_valid_tokens() -> Dict[str, Any]:
    tokens = read_json(TOKENS_PATH)
    if not isinstance(tokens, dict):
        raise RuntimeError(f"tokens file invalid: {TOKENS_PATH}")

    if token_is_expired(tokens):
        tokens = refresh_tokens(tokens)
    return tokens


# -----------------------------
# Auto-Assist runtime state + MQTT entity
# -----------------------------
def read_auto_assist_runtime() -> Dict[str, Any]:
    st = read_json(AUTO_ASSIST_STATE_PATH)
    if not isinstance(st, dict):
        st = {}
    st.setdefault("enabled", False)
    st.setdefault("last_run", None)
    st.setdefault("last_action", None)
    st.setdefault("last_error", None)
    return st


def boot_auto_assist_force_off() -> Dict[str, Any]:
    st = read_auto_assist_runtime()
    st["enabled"] = False
    st["last_action"] = "boot_off"
    st["last_error"] = None
    st["last_run"] = now_iso()
    write_json_atomic(AUTO_ASSIST_STATE_PATH, st)
    return st


def publish_auto_assist(mpub: MqttPub, cfg: Dict[str, Any], st: Dict[str, Any]) -> None:
    if not mpub.client:
        return

    tp = cfg["topic_prefix"]
    enabled = bool(st.get("enabled"))

    mpub.publish(f"{tp}/{AUTO_ASSIST_STATE_TOPIC}", "ON" if enabled else "OFF", retain=True)
    mpub.publish_json(
        f"{tp}/{AUTO_ASSIST_ATTRS_TOPIC}",
        {
            "enabled": enabled,
            "last_run": st.get("last_run"),
            "last_action": st.get("last_action"),
            "last_error": st.get("last_error"),
            "_ts": now_iso(),
        },
        retain=True,
    )


def publish_auto_assist_discovery(mpub: MqttPub, cfg: Dict[str, Any]) -> None:
    if not mpub.client:
        return

    dp = cfg["discovery_prefix"]
    tp = cfg["topic_prefix"]
    ha_device_name = cfg["ha_device_name"]
    ha_device_id = cfg["ha_device_id"]

    device_block = {
        "identifiers": [ha_device_id],
        "name": ha_device_name,
        "manufacturer": "tado°",
        "model": "Tado Assistant (Ingress)",
    }

    # Canonical discovery path: node_id + object_id
    node_id = ha_device_id
    object_id = "auto_assist"
    config_topic = f"{dp}/switch/{node_id}/{object_id}/config"

    payload = {
        "name": "Auto-Assist",
        "unique_id": f"{ha_device_id}_auto_assist",
        "state_topic": f"{tp}/{AUTO_ASSIST_STATE_TOPIC}",
        "command_topic": f"{tp}/{AUTO_ASSIST_SET_TOPIC}",
        "payload_on": "ON",
        "payload_off": "OFF",
        "state_on": "ON",
        "state_off": "OFF",
        "optimistic": False,
        "retain": True,

        "json_attributes_topic": f"{tp}/{AUTO_ASSIST_ATTRS_TOPIC}",
        "device": device_block,
        "icon": "mdi:robot",
    }

    mpub.publish_json(config_topic, payload, retain=True)


def cleanup_old_auto_assist_discovery(mpub: MqttPub, cfg: Dict[str, Any]) -> None:
    if not mpub.client:
        return

    dp = cfg["discovery_prefix"]
    ha_device_id = cfg["ha_device_id"]

    old_topics = [
        f"{dp}/switch/{ha_device_id}_auto_assist/config",
        f"{dp}/switch/{ha_device_id}/{ha_device_id}_auto_assist/config",
        f"{dp}/switch/{ha_device_id}/{ha_device_id}_auto_assist_switch/config",
        f"{dp}/switch/{ha_device_id}_tado_assistant_auto_assist/config",
    ]
    for t in old_topics:
        mpub.publish_delete_retained(t)


# -----------------------------
# Presence normalization / publish + discovery
# -----------------------------
def normalize_presence(device: Dict[str, Any]) -> Dict[str, Any]:
    name = device.get("name") or device.get("deviceName") or device.get("model") or "unknown"
    dev_id = device.get("id") or device.get("deviceId")

    at_home = None
    if isinstance(device.get("location"), dict) and "atHome" in device["location"]:
        at_home = bool(device["location"]["atHome"])
    elif "atHome" in device:
        at_home = bool(device.get("atHome"))

    if at_home is True:
        state = "home"
    elif at_home is False:
        state = "not_home"
    else:
        state = "unknown"

    return {"id": dev_id, "name": name, "state": state, "at_home": at_home, "_ts": now_iso(), "raw": device}


def discovery_object_ids(ha_device_id: str, device_id: int) -> Tuple[str, str, str]:
    tracker_object_id = f"{ha_device_id}_presence_{device_id}"
    raw_object_id = f"{ha_device_id}_presence_{device_id}_raw"
    old_json_object_id = f"{ha_device_id}_presence_{device_id}_json"
    return tracker_object_id, raw_object_id, old_json_object_id


def publish_discovery_for_devices(
    mpub: MqttPub,
    discovery_prefix: str,
    topic_prefix: str,
    ha_device_name: str,
    ha_device_id: str,
    home_id: int,
    devices: List[Dict[str, Any]],
    enable_raw_sensors: bool,
) -> None:
    if not mpub.client:
        return

    device_block = {
        "identifiers": [ha_device_id],
        "name": ha_device_name,
        "manufacturer": "tado°",
        "model": "Tado Assistant (Ingress)",
    }
    availability_topic = f"{topic_prefix}/_status"

    # Home raw (optional)
    if enable_raw_sensors:
        agg_object_id_new = f"{ha_device_id}_home_{home_id}_raw"
        agg_object_id_old = f"{ha_device_id}_home_{home_id}_presence"
        agg_config_topic_new = f"{discovery_prefix}/sensor/{agg_object_id_new}/config"
        agg_config_topic_old = f"{discovery_prefix}/sensor/{agg_object_id_old}/config"
        agg_topic = f"{topic_prefix}/presence/home_{home_id}/raw"

        mpub.publish_delete_retained(agg_config_topic_old)

        agg_payload = {
            "name": f"Tado Home {home_id} (raw)",
            "unique_id": f"{ha_device_id}_home_{home_id}_raw",
            "state_topic": agg_topic,
            "value_template": "{{ value_json._ts }}",
            "json_attributes_topic": agg_topic,
            "availability_topic": availability_topic,
            "payload_available": "online",
            "payload_not_available": "offline",
            "device": device_block,
            "icon": "mdi:home-account",
            "entity_category": "diagnostic",
        }
        mpub.publish_json(agg_config_topic_new, agg_payload, retain=True)

    for d in devices:
        did = d.get("id")
        name = d.get("name") or f"Device {did}"
        if not did:
            continue
        did_int = int(did)

        tracker_object_id, raw_object_id, old_json_object_id = discovery_object_ids(ha_device_id, did_int)

        state_topic = f"{topic_prefix}/presence/home_{home_id}/device_{did_int}/state"
        raw_topic = f"{topic_prefix}/presence/home_{home_id}/device_{did_int}/raw"

        mpub.publish_delete_retained(f"{discovery_prefix}/binary_sensor/{tracker_object_id}/config")

        tracker_config_topic = f"{discovery_prefix}/device_tracker/{tracker_object_id}/config"
        tracker_payload = {
            "name": f"Tado {name}",
            "unique_id": f"{ha_device_id}_home_{home_id}_device_{did_int}_tracker",
            "state_topic": state_topic,
            "payload_home": "home",
            "payload_not_home": "not_home",
            "source_type": "gps",
            "availability_topic": availability_topic,
            "payload_available": "online",
            "payload_not_available": "offline",
            "device": device_block,
        }
        mpub.publish_json(tracker_config_topic, tracker_payload, retain=True)

        if enable_raw_sensors:
            mpub.publish_delete_retained(f"{discovery_prefix}/sensor/{old_json_object_id}/config")

            raw_config_topic = f"{discovery_prefix}/sensor/{raw_object_id}/config"
            raw_payload = {
                "name": f"Tado {name} (raw)",
                "unique_id": f"{ha_device_id}_home_{home_id}_device_{did_int}_raw",
                "state_topic": state_topic,
                "json_attributes_topic": raw_topic,
                "availability_topic": availability_topic,
                "payload_available": "online",
                "payload_not_available": "offline",
                "device": device_block,
                "icon": "mdi:account",
                "entity_category": "diagnostic",
            }
            mpub.publish_json(raw_config_topic, raw_payload, retain=True)


def publish_presence(
    mpub: MqttPub,
    topic_prefix: str,
    home_id: int,
    devices: List[Dict[str, Any]],
    enable_raw_sensors: bool,
) -> None:
    for d in devices:
        did_int = int(d["id"])
        state_topic = f"{topic_prefix}/presence/home_{home_id}/device_{did_int}/state"
        raw_topic = f"{topic_prefix}/presence/home_{home_id}/device_{did_int}/raw"

        mpub.publish(state_topic, str(d.get("state", "unknown")), retain=True)
        if enable_raw_sensors:
            mpub.publish_json(raw_topic, d, retain=True)

    if enable_raw_sensors:
        agg_topic = f"{topic_prefix}/presence/home_{home_id}/raw"
        mpub.publish_json(agg_topic, {"_ts": now_iso(), "home_id": home_id, "devices": devices}, retain=True)

    cache = read_json(LAST_DEVICES_PATH)
    if not isinstance(cache, dict):
        cache = {}
    cache[str(home_id)] = {"_ts": now_iso(), "devices": devices}
    write_json_atomic(LAST_DEVICES_PATH, cache)


def republish_from_cache(mpub: MqttPub, cfg: Dict[str, Any]) -> None:
    cache = read_json(LAST_DEVICES_PATH)
    if not isinstance(cache, dict):
        return
    topic_prefix = cfg["topic_prefix"]
    enable_raw = bool(cfg.get("enable_raw_sensors", True))
    for home_id_s, payload in cache.items():
        try:
            home_id = int(home_id_s)
        except Exception:
            continue
        if isinstance(payload, dict) and isinstance(payload.get("devices"), list):
            devices = payload["devices"]
            try:
                publish_presence(mpub, topic_prefix, home_id, devices, enable_raw)
                log(f"republished cached presence home={home_id} devices={len(devices)}")
            except Exception:
                pass


def load_cached_home_ids() -> Optional[List[int]]:
    st = read_json(DISCOVERY_STATE_PATH)
    if isinstance(st, dict):
        hid = st.get("home_ids")
        if isinstance(hid, list):
            out: List[int] = []
            for x in hid:
                try:
                    out.append(int(x))
                except Exception:
                    pass
            return sorted(list(set(out))) if out else None
    return None


def save_cached_home_ids(home_ids: List[int]) -> None:
    st = read_json(DISCOVERY_STATE_PATH)
    if not isinstance(st, dict):
        st = {}
    st["home_ids"] = home_ids
    write_json_atomic(DISCOVERY_STATE_PATH, st)


# -----------------------------
# Main loop
# -----------------------------
def main() -> None:
    cfg = load_config()

    poll = int(cfg["poll_seconds"])
    topic_prefix = cfg["topic_prefix"]
    discovery_prefix = cfg["discovery_prefix"]
    ha_device_name = cfg["ha_device_name"]
    ha_device_id = cfg["ha_device_id"]
    enable_raw_sensors = bool(cfg.get("enable_raw_sensors", True))

    log(f"starting. poll_seconds={poll} enable_raw_sensors={enable_raw_sensors}")

    # Force OFF after every restart (your requirement)
    boot_auto_assist_force_off()

    mpub = MqttPub(cfg["mqtt"])

    def handle_mqtt_message(topic: str, payload: str) -> None:
        wanted = f"{topic_prefix}/{AUTO_ASSIST_SET_TOPIC}"
        if topic != wanted:
            return

        cmd = (payload or "").strip().upper()
        st = read_auto_assist_runtime()

        if cmd not in ("ON", "OFF"):
            st["last_error"] = f"invalid command payload: '{payload}'"
            st["last_action"] = "reject_command"
            st["last_run"] = now_iso()
            write_json_atomic(AUTO_ASSIST_STATE_PATH, st)
            publish_auto_assist(mpub, cfg, st)
            return

        st["enabled"] = (cmd == "ON")
        st["last_action"] = "switch_on" if st["enabled"] else "switch_off"
        st["last_error"] = None
        st["last_run"] = now_iso()
        write_json_atomic(AUTO_ASSIST_STATE_PATH, st)
        publish_auto_assist(mpub, cfg, st)
        log(f"Auto-Assist switched {cmd}")

    mpub.set_on_message(handle_mqtt_message)

    def handle_connect() -> None:
        # Availability first
        mpub.publish(f"{topic_prefix}/_status", "online", retain=True)

        # Remove old retained switch configs so HA stops using them
        cleanup_old_auto_assist_discovery(mpub, cfg)

        # Publish new switch discovery + initial state/attrs
        publish_auto_assist_discovery(mpub, cfg)
        publish_auto_assist(mpub, cfg, read_auto_assist_runtime())

        # Subscribe to HA commands
        mpub.subscribe(f"{topic_prefix}/{AUTO_ASSIST_SET_TOPIC}")

        log("auto-assist: discovery+state published; command subscribed")

    mpub.set_on_connect(handle_connect)

    # LWT marks offline if addon dies
    mpub.start(lwt_topic=f"{topic_prefix}/_status")

    republish_from_cache(mpub, cfg)

    explicit_home_ids: Optional[List[int]] = cfg.get("tado_home_ids")
    home_ids: Optional[List[int]] = explicit_home_ids or load_cached_home_ids()

    backoff_base = int(cfg["rate_limit_backoff_seconds"])
    backoff_max = int(cfg["rate_limit_backoff_max_seconds"])
    backoff_current = backoff_base

    loop = 0

    while True:
        loop += 1
        try:
            if not tokens_exist():
                log(f"waiting for tokens: {TOKENS_PATH} (login first)")
                time.sleep(5)
                continue

            tokens = ensure_valid_tokens()
            access_token = tokens["access_token"]

            if home_ids is None:
                home_ids = get_home_ids(access_token)
                save_cached_home_ids(home_ids)
                log(f"home ids resolved: {home_ids}")

            for home_id in home_ids:
                devices_raw = get_mobile_devices(access_token, home_id)
                devices = [normalize_presence(d) for d in devices_raw]

                if mpub.client and (loop == 1 or loop % DISCOVERY_REPUBLISH_EVERY_LOOPS == 0):
                    publish_discovery_for_devices(
                        mpub,
                        discovery_prefix,
                        topic_prefix,
                        ha_device_name,
                        ha_device_id,
                        home_id,
                        devices,
                        enable_raw_sensors,
                    )

                publish_presence(mpub, topic_prefix, home_id, devices, enable_raw_sensors)
                log(f"presence updated home={home_id} devices={len(devices)}")

            # Auto-Assist heartbeat metadata when enabled (real actions come next)
            st = read_auto_assist_runtime()
            if st.get("enabled") is True:
                st["last_run"] = now_iso()
                st["last_action"] = "tick"
                st["last_error"] = None
                write_json_atomic(AUTO_ASSIST_STATE_PATH, st)
                publish_auto_assist(mpub, cfg, st)

            backoff_current = backoff_base
            time.sleep(poll)

        except RateLimitError as e:
            sleep_s = e.retry_after if e.retry_after is not None else backoff_current
            sleep_s = max(5, int(sleep_s))
            sleep_s = min(sleep_s, backoff_max)
            log(f"WARN: Tado rate limit (429) on {e.path} -> backoff {sleep_s}s")

            republish_from_cache(mpub, cfg)

            backoff_current = min(backoff_current * 2, backoff_max)
            time.sleep(sleep_s)

        except Exception as e:
            try:
                st = read_auto_assist_runtime()
                st["last_run"] = now_iso()
                st["last_action"] = "error"
                st["last_error"] = str(e)
                write_json_atomic(AUTO_ASSIST_STATE_PATH, st)
                publish_auto_assist(mpub, cfg, st)
            except Exception:
                pass

            log(f"ERROR: {e}")
            time.sleep(10)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("stopped by user")
        sys.exit(0)
    except Exception as e:
        log(f"FATAL: {e}")
        sys.exit(1)
