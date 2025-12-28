import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple, Callable

import requests

try:
    import paho.mqtt.client as mqtt  # type: ignore
except Exception:
    mqtt = None  # type: ignore


TOKENS_PATH = "/data/tado_assistant_tokens.json"
CACHE_DIR = "/data/cache"

AUTO_ASSIST_STATE_PATH = "/data/auto_assist_state.json"
DISCOVERY_STATE_PATH = os.path.join(CACHE_DIR, "discovery_state.json")


DEFAULT_OPTIONS_PATHS = [
    "/data/options.json",  # HA add-on standard
    "/data/config.json",
]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def log(msg: str) -> None:
    print(f"[worker] {msg}", flush=True)


def read_json(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def write_json_atomic(path: str, obj: Any) -> None:
    tmp = f"{path}.tmp"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def load_options() -> Dict[str, Any]:
    for p in DEFAULT_OPTIONS_PATHS:
        if os.path.exists(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
    return {}


def tokens_exist() -> bool:
    return os.path.exists(TOKENS_PATH)


class RateLimitError(Exception):
    def __init__(self, path: str, retry_after: Optional[int]):
        super().__init__(f"rate limit on {path}")
        self.path = path
        self.retry_after = retry_after


def parse_retry_after(headers: Dict[str, Any]) -> Optional[int]:
    ra = headers.get("Retry-After")
    if ra is None:
        return None
    try:
        return int(str(ra).strip())
    except Exception:
        return None


def load_config() -> Dict[str, Any]:
    opt = load_options()

    poll = opt.get("poll_seconds", opt.get("poll", 30))
    try:
        poll = int(poll)
    except Exception:
        poll = 30
    poll = max(10, poll)

    enable_raw_sensors = opt.get("enable_raw_sensors", True)
    enable_raw_sensors = bool(enable_raw_sensors)

    enable_open_window = opt.get("enable_open_window", True)
    enable_open_window = bool(enable_open_window)

    max_open_window_duration = opt.get("max_open_window_duration", opt.get("max_open_window_seconds"))
    if max_open_window_duration in ("", None):
        max_open_window_duration = None
    else:
        try:
            max_open_window_duration = int(max_open_window_duration)
        except Exception:
            max_open_window_duration = None
    if isinstance(max_open_window_duration, int) and max_open_window_duration <= 0:
        max_open_window_duration = None

    # Optional: explicitly set home ids
    tado_home_ids = opt.get("tado_home_ids", opt.get("home_ids"))
    if isinstance(tado_home_ids, list):
        try:
            tado_home_ids = [int(x) for x in tado_home_ids]
        except Exception:
            tado_home_ids = None
    elif tado_home_ids in ("", None):
        tado_home_ids = None
    else:
        try:
            tado_home_ids = [int(tado_home_ids)]
        except Exception:
            tado_home_ids = None

    rate_backoff = opt.get("rate_limit_backoff_seconds", 60)
    rate_backoff_max = opt.get("rate_limit_backoff_max_seconds", 600)
    try:
        rate_backoff = int(rate_backoff)
    except Exception:
        rate_backoff = 60
    try:
        rate_backoff_max = int(rate_backoff_max)
    except Exception:
        rate_backoff_max = 600
    rate_backoff = max(10, rate_backoff)
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

    ha_device_name = opt.get("ha_device_name") or "Tado Assistant (Ingress)"
    ha_device_id = opt.get("ha_device_id") or "tado_assistant"

    return {
        "poll_seconds": poll,
        "enable_raw_sensors": enable_raw_sensors,
        "enable_open_window": enable_open_window,
        "max_open_window_duration": max_open_window_duration,
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
        self.cfg = cfg
        self.client = None
        self.connected = False
        self.last_will_set = False
        self._subscriptions: Dict[str, Callable[[str, str], None]] = {}

    def connect(self) -> None:
        mcfg = self.cfg.get("mqtt", {})
        if not isinstance(mcfg, dict):
            mcfg = {}
        enabled = bool(mcfg.get("enabled", True))
        if not enabled:
            log("MQTT disabled by config")
            return

        if mqtt is None:
            raise RuntimeError("paho-mqtt not installed")

        host = str(mcfg.get("host") or os.getenv("MQTT_HOST") or "core-mosquitto")
        port = int(mcfg.get("port") or os.getenv("MQTT_PORT") or 1883)
        username = mcfg.get("username") or os.getenv("MQTT_USERNAME")
        password = mcfg.get("password") or os.getenv("MQTT_PASSWORD")
        tls = bool(mcfg.get("tls", False))

        client_id = mcfg.get("client_id") or f"{self.cfg.get('ha_device_id','tado_assistant')}_{int(time.time())}"

        log(f"MQTT connecting to {host}:{port} (tls={tls}, user={'yes' if username else 'no'})")

        c = mqtt.Client(client_id=client_id, clean_session=True)

        if username:
            c.username_pw_set(str(username), str(password) if password is not None else None)

        if tls:
            c.tls_set()

        def on_connect(client, userdata, flags, rc):
            self.connected = (rc == 0)
            log(f"MQTT connected rc={rc}")

        def on_disconnect(client, userdata, rc):
            self.connected = False
            log(f"MQTT disconnected rc={rc}")

        def on_message(client, userdata, msg):
            try:
                topic = msg.topic
                payload = msg.payload.decode("utf-8", errors="ignore")
                cb = self._subscriptions.get(topic)
                if cb:
                    cb(topic, payload)
            except Exception as e:
                log(f"ERROR on_message: {e}")

        c.on_connect = on_connect
        c.on_disconnect = on_disconnect
        c.on_message = on_message

        # LWT
        status_topic = f"{self.cfg['topic_prefix']}/_status"
        c.will_set(status_topic, payload="offline", qos=1, retain=True)
        self.last_will_set = True

        c.connect(host, port, keepalive=60)
        c.loop_start()

        self.client = c

        # publish online immediately (retain)
        self.publish(status_topic, "online", retain=True)
        log(f"status published: {status_topic} = online")

    def publish(self, topic: str, payload: str, retain: bool = False, qos: int = 1) -> None:
        if not self.client:
            return
        self.client.publish(topic, payload=payload, qos=qos, retain=retain)

    def publish_json(self, topic: str, obj: Any, retain: bool = False, qos: int = 1) -> None:
        payload = json.dumps(obj, ensure_ascii=False)
        self.publish(topic, payload, retain=retain, qos=qos)

    def subscribe(self, topic: str, callback: Callable[[str, str], None]) -> None:
        if not self.client:
            return
        self._subscriptions[topic] = callback
        self.client.subscribe(topic, qos=1)


# -----------------------------
# HTTP / Tado
# -----------------------------
def tado_base_url() -> str:
    return os.getenv("TADO_API_BASE_URL", "https://my.tado.com").rstrip("/")


def api_request(method: str, path: str, access_token: str, json_body: Any = None) -> Tuple[int, Any, Dict[str, Any]]:
    url = f"{tado_base_url()}{path}"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "User-Agent": "tado-assistant/1.0",
    }
    try:
        r = requests.request(method, url, headers=headers, json=json_body, timeout=30)
    except Exception as e:
        raise RuntimeError(f"HTTP error on {path}: {e}")

    try:
        data = r.json() if r.text else None
    except Exception:
        data = r.text

    return r.status_code, data, dict(r.headers)


def get_home_ids(access_token: str) -> List[int]:
    path = "/api/v2/homes"
    status, data, headers = api_request("GET", path, access_token)
    if status == 429:
        raise RateLimitError(path, parse_retry_after(headers))
    if status != 200:
        raise RuntimeError(f"Tado API {status} on {path}: {data}")
    ids: List[int] = []
    if isinstance(data, list):
        for h in data:
            try:
                ids.append(int(h.get("id")))
            except Exception:
                pass
    return ids


def get_mobile_devices(access_token: str, home_id: int) -> List[Dict[str, Any]]:
    path = f"/api/v2/homes/{home_id}/mobileDevices"
    status, data, headers = api_request("GET", path, access_token)
    if status == 429:
        raise RateLimitError(path, parse_retry_after(headers))
    if status != 200:
        raise RuntimeError(f"Tado API {status} on {path}: {data}")
    return data if isinstance(data, list) else []


# -----------------------------
# Open Window (zones)
# -----------------------------
def get_zones(access_token: str, home_id: int) -> List[Dict[str, Any]]:
    path = f"/homes/{home_id}/zones"
    status, data, headers = api_request("GET", path, access_token)
    if status == 429:
        raise RateLimitError(path, parse_retry_after(headers))
    if status != 200:
        raise RuntimeError(f"Tado API {status} on {path}: {data}")
    return data if isinstance(data, list) else []


def get_zone_state(access_token: str, home_id: int, zone_id: int) -> Dict[str, Any]:
    path = f"/homes/{home_id}/zones/{zone_id}/state"
    status, data, headers = api_request("GET", path, access_token)
    if status == 429:
        raise RateLimitError(path, parse_retry_after(headers))
    if status != 200:
        raise RuntimeError(f"Tado API {status} on {path}: {data}")
    return data if isinstance(data, dict) else {}


def zone_open_window_detected(zone_state: Dict[str, Any]) -> bool:
    ow = zone_state.get("openWindowDetected")
    if isinstance(ow, bool):
        return ow
    if isinstance(ow, str):
        return ow.strip().lower() == "true"
    if isinstance(ow, dict):
        v = ow.get("detected")
        if isinstance(v, bool):
            return v
        v = ow.get("value")
        if isinstance(v, bool):
            return v
        v = ow.get("openWindowDetected")
        if isinstance(v, bool):
            return v
    return False


def activate_open_window(access_token: str, home_id: int, zone_id: int) -> None:
    path = f"/homes/{home_id}/zones/{zone_id}/state/openWindow/activate"
    status, data, headers = api_request("POST", path, access_token)
    if status == 429:
        raise RateLimitError(path, parse_retry_after(headers))
    if status not in (200, 204):
        raise RuntimeError(f"Tado API {status} on {path}: {data}")


def cancel_open_window(access_token: str, home_id: int, zone_id: int) -> None:
    path = f"/homes/{home_id}/zones/{zone_id}/state/openWindow"
    status, data, headers = api_request("DELETE", path, access_token)
    if status == 429:
        raise RateLimitError(path, parse_retry_after(headers))
    if status not in (200, 204):
        raise RuntimeError(f"Tado API {status} on {path}: {data}")


# -----------------------------
# Token handling
# -----------------------------
def ensure_valid_tokens() -> Dict[str, Any]:
    tokens = read_json(TOKENS_PATH)
    if not tokens:
        raise RuntimeError("tokens file missing or invalid JSON")

    access_token = tokens.get("access_token")
    if not access_token:
        raise RuntimeError("access_token missing in tokens file")

    # refresh if expiring soon (best-effort)
    expires_at = tokens.get("expires_at")
    if expires_at:
        try:
            expires_at = float(expires_at)
        except Exception:
            expires_at = None

    if expires_at and (expires_at - time.time()) < 60:
        refresh_token = tokens.get("refresh_token")
        client_id = tokens.get("client_id")
        client_secret = tokens.get("client_secret")
        if not refresh_token or not client_id or not client_secret:
            log("ERROR: refresh_token/client_id/client_secret missing in tokens file")
            return tokens

        log("token needs refresh -> refreshing")
        newt = refresh_tokens(str(refresh_token), str(client_id), str(client_secret))
        tokens.update(newt)
        write_json_atomic(TOKENS_PATH, tokens)

    return tokens


def refresh_tokens(refresh_token: str, client_id: str, client_secret: str) -> Dict[str, Any]:
    url = f"{tado_base_url()}/oauth/token"
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
        "client_secret": client_secret,
    }
    r = requests.post(url, data=data, timeout=30)
    try:
        j = r.json()
    except Exception:
        j = {"raw": r.text}

    if r.status_code != 200:
        raise RuntimeError(f"token refresh failed {r.status_code}: {j}")

    out = {
        "access_token": j.get("access_token"),
        "refresh_token": j.get("refresh_token", refresh_token),
    }
    expires_in = j.get("expires_in")
    try:
        expires_in = int(expires_in)
    except Exception:
        expires_in = 3600
    out["expires_at"] = time.time() + expires_in
    return out


# -----------------------------
# Presence normalization / publish + discovery
# -----------------------------
def normalize_presence(device: Dict[str, Any]) -> Dict[str, Any]:
    name = device.get("name") or f"device_{device.get('id')}"
    did = device.get("id") or name
    at_home = False
    loc = device.get("location")
    if isinstance(loc, dict):
        at_home = bool(loc.get("atHome", False))
    return {
        "id": did,
        "name": name,
        "at_home": at_home,
        "raw": device,
    }


def safe_object_id(s: str) -> str:
    out = []
    for ch in s.lower():
        if ch.isalnum():
            out.append(ch)
        elif ch in (" ", "-", "_"):
            out.append("_")
    v = "".join(out).strip("_")
    while "__" in v:
        v = v.replace("__", "_")
    return v or "unknown"


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

    for d in devices:
        name = str(d.get("name") or d.get("id"))
        oid = safe_object_id(name)
        did = str(d.get("id") or oid)

        # device_tracker
        node_id = ha_device_id
        object_id = f"presence_{home_id}_{oid}"
        config_topic = f"{discovery_prefix}/device_tracker/{node_id}/{object_id}/config"
        state_topic = f"{topic_prefix}/presence/{home_id}/{did}/state"

        payload = {
            "name": f"Tado {name}",
            "unique_id": f"{ha_device_id}_home_{home_id}_{did}_tracker",
            "state_topic": state_topic,
            "payload_home": "home",
            "payload_not_home": "not_home",
            "source_type": "gps",
            "device": device_block,
        }
        mpub.publish_json(config_topic, payload, retain=True)

        # raw sensor
        if enable_raw_sensors:
            object_id2 = f"presence_{home_id}_{oid}_raw"
            config_topic2 = f"{discovery_prefix}/sensor/{node_id}/{object_id2}/config"
            state_topic2 = f"{topic_prefix}/presence/{home_id}/{did}/raw"

            payload2 = {
                "name": f"Tado {name} (raw)",
                "unique_id": f"{ha_device_id}_home_{home_id}_{did}_raw",
                "state_topic": state_topic2,
                "value_template": "{{ value_json.timestamp }}",
                "json_attributes_topic": state_topic2,
                "device": device_block,
                "entity_category": "diagnostic",
                "enabled_by_default": False,
            }
            mpub.publish_json(config_topic2, payload2, retain=True)

    # home raw sensor (to get back old count style)
    if enable_raw_sensors:
        node_id = ha_device_id
        object_id = f"home_{home_id}_raw"
        config_topic = f"{discovery_prefix}/sensor/{node_id}/{object_id}/config"
        state_topic = f"{topic_prefix}/presence/{home_id}/home_raw"
        payload = {
            "name": f"Tado Home {home_id} (raw)",
            "unique_id": f"{ha_device_id}_home_{home_id}_raw",
            "state_topic": state_topic,
            "value_template": "{{ value_json.timestamp }}",
            "json_attributes_topic": state_topic,
            "device": device_block,
            "entity_category": "diagnostic",
            "enabled_by_default": False,
        }
        mpub.publish_json(config_topic, payload, retain=True)


def publish_presence(
    mpub: MqttPub,
    topic_prefix: str,
    home_id: int,
    devices: List[Dict[str, Any]],
    enable_raw_sensors: bool,
) -> None:
    ts = now_iso()
    home_raw: Dict[str, Any] = {"timestamp": ts, "home_id": home_id, "devices": []}

    for d in devices:
        did = str(d.get("id"))
        at_home = bool(d.get("at_home"))
        state_topic = f"{topic_prefix}/presence/{home_id}/{did}/state"
        mpub.publish(state_topic, "home" if at_home else "not_home", retain=True)

        if enable_raw_sensors:
            raw_topic = f"{topic_prefix}/presence/{home_id}/{did}/raw"
            raw_payload = {"timestamp": ts, **(d.get("raw") or {})}
            mpub.publish_json(raw_topic, raw_payload, retain=True)

        home_raw["devices"].append({"id": did, "name": d.get("name"), "at_home": at_home})

    if enable_raw_sensors:
        mpub.publish_json(f"{topic_prefix}/presence/{home_id}/home_raw", home_raw, retain=True)


def publish_open_window_discovery_for_home(
    mpub: MqttPub,
    discovery_prefix: str,
    topic_prefix: str,
    ha_device_name: str,
    ha_device_id: str,
    home_id: int,
    zones: List[Dict[str, Any]],
) -> List[Tuple[int, str]]:
    """Publish MQTT Discovery for zone open-window sensors.
    Returns list of (zone_id, zone_name) that were published.
    """
    if not mpub.client:
        return []

    device_block = {
        "identifiers": [ha_device_id],
        "name": ha_device_name,
        "manufacturer": "tado°",
        "model": "Tado Assistant (Ingress)",
    }
    availability_topic = f"{topic_prefix}/_status"

    published: List[Tuple[int, str]] = []

    for z in zones:
        if not isinstance(z, dict):
            continue

        zid = z.get("id")
        zname = z.get("name") or f"Zone {zid}"
        try:
            zid_int = int(zid)
        except Exception:
            continue

        # Only zones where Open Window detection is supported & enabled
        owd = z.get("openWindowDetection")
        supported = True
        enabled = True
        if isinstance(owd, dict):
            if "supported" in owd:
                supported = bool(owd.get("supported"))
            if "enabled" in owd:
                enabled = bool(owd.get("enabled"))
        if not supported or not enabled:
            continue

        node_id = ha_device_id
        object_id = f"open_window_{home_id}_{zid_int}"
        config_topic = f"{discovery_prefix}/binary_sensor/{node_id}/{object_id}/config"

        state_topic = f"{topic_prefix}/open_window/home_{home_id}/zone_{zid_int}/state"

        payload = {
            "name": f"{zname} – Open Window",
            "unique_id": f"{ha_device_id}_home_{home_id}_zone_{zid_int}_open_window",
            "state_topic": state_topic,
            "payload_on": "ON",
            "payload_off": "OFF",
            "device_class": "window",
            "icon": "mdi:window-open-variant",
            "availability_topic": availability_topic,
            "payload_available": "online",
            "payload_not_available": "offline",
            "device": device_block,
        }
        mpub.publish_json(config_topic, payload, retain=True)
        published.append((zid_int, str(zname)))

    return published


def publish_open_window_states(
    mpub: MqttPub,
    topic_prefix: str,
    home_id: int,
    zone_states: List[Tuple[int, str, Dict[str, Any]]],
) -> None:
    """Publish ON/OFF window state per zone."""
    for zid, _zname, zstate in zone_states:
        detected = zone_open_window_detected(zstate)
        state_topic = f"{topic_prefix}/open_window/home_{home_id}/zone_{zid}/state"
        mpub.publish(state_topic, "ON" if detected else "OFF", retain=True)


# -----------------------------
# Auto-Assist Switch (MQTT Discovery + state + command)
# -----------------------------
def read_auto_assist_runtime() -> Dict[str, Any]:
    st = read_json(AUTO_ASSIST_STATE_PATH)
    if not st:
        st = {"enabled": False, "last_run": None, "last_action": None, "last_error": None}
    if "enabled" not in st:
        st["enabled"] = False
    return st


def publish_auto_assist_discovery(mpub: MqttPub, cfg: Dict[str, Any]) -> None:
    if not mpub.client:
        return

    discovery_prefix = cfg["discovery_prefix"]
    topic_prefix = cfg["topic_prefix"]
    ha_device_name = cfg["ha_device_name"]
    ha_device_id = cfg["ha_device_id"]

    device_block = {
        "identifiers": [ha_device_id],
        "name": ha_device_name,
        "manufacturer": "tado°",
        "model": "Tado Assistant (Ingress)",
    }

    config_topic = f"{discovery_prefix}/switch/{ha_device_id}/auto_assist/config"

    state_topic = f"{topic_prefix}/auto_assist/state"
    command_topic = f"{topic_prefix}/auto_assist/set"
    attrs_topic = f"{topic_prefix}/auto_assist/attrs"

    payload = {
        "name": "Auto-Assist",
        "unique_id": f"{ha_device_id}_auto_assist_switch",
        "state_topic": state_topic,
        "command_topic": command_topic,
        "payload_on": "ON",
        "payload_off": "OFF",
        "state_on": "ON",
        "state_off": "OFF",
        "icon": "mdi:thermostat-auto",
        "json_attributes_topic": attrs_topic,
        "device": device_block,
    }

    mpub.publish_json(config_topic, payload, retain=True)
    log(f"auto_assist discovery published: {config_topic}")


def publish_auto_assist(mpub: MqttPub, cfg: Dict[str, Any], st: Dict[str, Any]) -> None:
    topic_prefix = cfg["topic_prefix"]
    state_topic = f"{topic_prefix}/auto_assist/state"
    attrs_topic = f"{topic_prefix}/auto_assist/attrs"

    mpub.publish(state_topic, "ON" if st.get("enabled") else "OFF", retain=True)
    mpub.publish_json(attrs_topic, st, retain=True)


def setup_auto_assist_switch(mpub: MqttPub, cfg: Dict[str, Any]) -> None:
    topic_prefix = cfg["topic_prefix"]
    command_topic = f"{topic_prefix}/auto_assist/set"

    def on_cmd(topic: str, payload: str) -> None:
        val = payload.strip().upper()
        enabled = (val == "ON" or val == "1" or val == "TRUE")
        st = read_auto_assist_runtime()
        st["enabled"] = enabled
        st["last_run"] = now_iso()
        st["last_action"] = "enabled" if enabled else "disabled"
        st["last_error"] = None
        write_json_atomic(AUTO_ASSIST_STATE_PATH, st)
        publish_auto_assist(mpub, cfg, st)
        log(f"auto_assist set to {enabled}")

    mpub.subscribe(command_topic, on_cmd)
    log(f"auto_assist subscribed: {command_topic}")


# -----------------------------
# Cache helpers
# -----------------------------
def save_cached_home_ids(home_ids: List[int]) -> None:
    os.makedirs(CACHE_DIR, exist_ok=True)
    write_json_atomic(os.path.join(CACHE_DIR, "home_ids.json"), {"home_ids": home_ids})


def load_cached_home_ids() -> Optional[List[int]]:
    p = os.path.join(CACHE_DIR, "home_ids.json")
    d = read_json(p)
    if not d:
        return None
    v = d.get("home_ids")
    if isinstance(v, list):
        out: List[int] = []
        for x in v:
            try:
                out.append(int(x))
            except Exception:
                pass
        return out
    return None


def republish_from_cache(mpub: MqttPub, cfg: Dict[str, Any]) -> None:
    # minimal: republish auto-assist discovery so HA doesn't lose it after downtime
    try:
        publish_auto_assist_discovery(mpub, cfg)
        publish_auto_assist(mpub, cfg, read_auto_assist_runtime())
    except Exception:
        pass


DISCOVERY_REPUBLISH_EVERY_LOOPS = 20


def main() -> None:
    cfg = load_config()
    poll = int(cfg["poll_seconds"])
    enable_raw_sensors = bool(cfg.get("enable_raw_sensors", True))
    enable_open_window = bool(cfg.get("enable_open_window", True))
    max_open_window_duration = cfg.get("max_open_window_duration")

    mpub = MqttPub(cfg)
    mpub.connect()

    discovery_prefix = cfg["discovery_prefix"]
    topic_prefix = cfg["topic_prefix"]
    ha_device_name = cfg["ha_device_name"]
    ha_device_id = cfg["ha_device_id"]

    # publish discovery for auto-assist switch
    publish_auto_assist_discovery(mpub, cfg)
    setup_auto_assist_switch(mpub, cfg)
    publish_auto_assist(mpub, cfg, read_auto_assist_runtime())

    home_ids = cfg.get("tado_home_ids") or load_cached_home_ids()

    backoff_base = int(cfg["rate_limit_backoff_seconds"])
    backoff_max = int(cfg["rate_limit_backoff_max_seconds"])
    backoff_current = backoff_base

    loop = 0

    # Track open-window activations (home_id, zone_id) -> epoch seconds
    open_window_activations: Dict[Tuple[int, int], int] = {}

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

                # Open Window sensors + (optional) Auto-Assist actions
                if enable_open_window:
                    zones = get_zones(access_token, home_id)
                    zones_to_check: List[Tuple[int, str]] = []
                    for z in zones:
                        if not isinstance(z, dict):
                            continue
                        zid = z.get("id")
                        zname = z.get("name") or f"Zone {zid}"
                        try:
                            zid_int = int(zid)
                        except Exception:
                            continue
                        owd = z.get("openWindowDetection")
                        supported = True
                        enabled = True
                        if isinstance(owd, dict):
                            if "supported" in owd:
                                supported = bool(owd.get("supported"))
                            if "enabled" in owd:
                                enabled = bool(owd.get("enabled"))
                        if not supported or not enabled:
                            continue
                        zones_to_check.append((zid_int, str(zname)))

                    if mpub.client and (loop == 1 or loop % DISCOVERY_REPUBLISH_EVERY_LOOPS == 0):
                        publish_open_window_discovery_for_home(
                            mpub,
                            discovery_prefix,
                            topic_prefix,
                            ha_device_name,
                            ha_device_id,
                            home_id,
                            zones,
                        )

                    zone_states: List[Tuple[int, str, Dict[str, Any]]] = []
                    for zid_int, zname in zones_to_check:
                        zstate = get_zone_state(access_token, home_id, zid_int)
                        zone_states.append((zid_int, zname, zstate))

                    publish_open_window_states(mpub, topic_prefix, home_id, zone_states)

                    # Auto-Assist window actions (activate/cancel) when enabled
                    st_local = read_auto_assist_runtime()
                    if st_local.get("enabled") is True:
                        changed = False
                        now_epoch = int(time.time())

                        for zid_int, zname, zstate in zone_states:
                            detected = zone_open_window_detected(zstate)
                            key = (home_id, zid_int)

                            try:
                                if detected:
                                    # Cancel after max duration (if configured)
                                    if isinstance(max_open_window_duration, int) and key in open_window_activations:
                                        if (now_epoch - open_window_activations[key]) > max_open_window_duration:
                                            cancel_open_window(access_token, home_id, zid_int)
                                            open_window_activations.pop(key, None)
                                            st_local["last_action"] = f"open_window_cancel:{zname}"
                                            st_local["last_run"] = now_iso()
                                            st_local["last_error"] = None
                                            changed = True
                                            continue

                                    # Activate (once) when detected
                                    if key not in open_window_activations:
                                        activate_open_window(access_token, home_id, zid_int)
                                        open_window_activations[key] = now_epoch
                                        st_local["last_action"] = f"open_window_activate:{zname}"
                                        st_local["last_run"] = now_iso()
                                        st_local["last_error"] = None
                                        changed = True
                                else:
                                    # Reset activation marker when window is closed
                                    if key in open_window_activations:
                                        open_window_activations.pop(key, None)
                            except Exception as e:
                                st_local["last_run"] = now_iso()
                                st_local["last_action"] = f"open_window_error:{zname}"
                                st_local["last_error"] = str(e)
                                changed = True

                        if changed:
                            write_json_atomic(AUTO_ASSIST_STATE_PATH, st_local)
                            publish_auto_assist(mpub, cfg, st_local)

            # Auto-Assist heartbeat metadata when enabled (real actions come next)
            st = read_auto_assist_runtime()
            if st.get("enabled") is True:
                # heartbeat: update last_run, but keep last_action/last_error as the last meaningful values
                st["last_run"] = now_iso()
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
        sys.exit(0)
