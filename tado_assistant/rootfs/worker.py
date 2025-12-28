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

# Auto-Assist state (enabled toggled by HA switch)
AUTO_ASSIST_STATE_PATH = os.path.join(DATA_DIR, "tado_assistant_auto_assist_state.json")

# One-time retained cleanup marker
CLEANUP_MARKER_PATH = os.path.join(DATA_DIR, "tado_assistant_cleanup_marker.json")

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
# Helpers
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

    enable_raw_sensors = bool(opt.get("enable_raw_sensors", True))

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

    # flat keys (support both styles)
    if "mqtt_enabled" in opt:
        mcfg["enabled"] = bool(opt.get("mqtt_enabled"))
    mcfg.setdefault("enabled", True)

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

    # Legacy cleanup: default TRUE once, can be turned off
    cleanup_legacy = bool(opt.get("cleanup_legacy_discovery", True))

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
        "cleanup_legacy_discovery": cleanup_legacy,
        "raw": opt,
    }


# -----------------------------
# MQTT wrapper
# -----------------------------
class MqttPub:
    def __init__(self, mqtt_cfg: Dict[str, Any]) -> None:
        self.cfg = mqtt_cfg or {}
        self.client = None
        self._on_message_cb: Optional[Callable[[str, str], None]] = None
        self._on_connect_cb: Optional[Callable[[], None]] = None

    def set_on_message(self, cb: Callable[[str, str], None]) -> None:
        self._on_message_cb = cb

    def set_on_connect(self, cb: Callable[[], None]) -> None:
        self._on_connect_cb = cb

    def start(self, lwt_topic: Optional[str] = None) -> None:
        enabled = bool(self.cfg.get("enabled", True))
        if not enabled:
            log("MQTT disabled (options.json mqtt_enabled=false)")
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

        # Last Will: if the add-on crashes, HA sees "offline"
        if lwt_topic:
            try:
                c.will_set(lwt_topic, payload="offline", qos=0, retain=True)
            except Exception as e:
                log(f"WARN: cannot set MQTT LWT: {e}")

        if username:
            c.username_pw_set(username, password=password)
        if tls:
            try:
                c.tls_set()
            except Exception as e:
                log(f"WARN: cannot enable MQTT TLS: {e}")

        def on_connect(client, userdata, flags, rc):
            log(f"MQTT connected rc={rc}")
            try:
                if self._on_connect_cb:
                    self._on_connect_cb()
            except Exception as e:
                log(f"WARN: on_connect callback failed: {e}")

        def on_disconnect(client, userdata, rc):
            log(f"MQTT disconnected rc={rc}")

        def on_message(client, userdata, msg):
            try:
                topic = msg.topic or ""
                payload = msg.payload.decode("utf-8", errors="ignore") if msg.payload else ""
                if self._on_message_cb:
                    self._on_message_cb(topic, payload)
            except Exception:
                pass

        c.on_connect = on_connect
        c.on_disconnect = on_disconnect
        c.on_message = on_message

        log(f"MQTT connecting to {host}:{port} (tls={tls}, user={'yes' if username else 'no'})")
        c.connect(host, port, keepalive=30)
        c.loop_start()
        self.client = c

    def subscribe(self, topic: str) -> None:
        if self.client:
            self.client.subscribe(topic)

    def publish(self, topic: str, payload: str, retain: bool = True) -> None:
        if self.client:
            self.client.publish(topic, payload, qos=0, retain=retain)

    def publish_json(self, topic: str, payload: Any, retain: bool = True) -> None:
        self.publish(topic, json.dumps(payload, ensure_ascii=False), retain=retain)

    def publish_delete_retained(self, topic: str) -> None:
        # publish empty retained payload => broker deletes retained message
        self.publish(topic, "", retain=True)


# -----------------------------
# Tado API
# -----------------------------
class RateLimitError(Exception):
    def __init__(self, path: str, retry_after: Optional[int] = None):
        super().__init__(path)
        self.path = path
        self.retry_after = retry_after


def api_request(method: str, path: str, access_token: str) -> Tuple[int, Any, Dict[str, str]]:
    url = f"{API_BASE}{path}"
    headers = {"Authorization": f"Bearer {access_token}"}
    r = requests.request(method, url, headers=headers, timeout=HTTP_TIMEOUT)
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
    if not s:
        return None
    try:
        return int(datetime.fromisoformat(s).timestamp())
    except Exception:
        return None


def token_is_expired(tokens: Dict[str, Any], skew_seconds: int = 60) -> bool:
    obtained = _obtained_epoch(tokens)
    try:
        expires_in = int(tokens.get("expires_in"))
    except Exception:
        return True
    if obtained is None:
        return True
    return time.time() >= (obtained + expires_in - skew_seconds)


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
# Auto-Assist: state + MQTT discovery
# -----------------------------
def load_auto_assist_state(force_off_on_boot: bool = True) -> Dict[str, Any]:
    st = read_json(AUTO_ASSIST_STATE_PATH)
    if not isinstance(st, dict):
        st = {}

    # Always have keys
    st.setdefault("enabled", False)
    st.setdefault("last_run", None)
    st.setdefault("last_action", None)
    st.setdefault("last_error", None)

    # Requirement: default OFF after restart
    if force_off_on_boot:
        st["enabled"] = False
        st["last_run"] = now_iso()
        st["last_action"] = "boot_off"
        st["last_error"] = None

    return st


def save_auto_assist_state(st: Dict[str, Any]) -> None:
    write_json_atomic(AUTO_ASSIST_STATE_PATH, st)


def publish_auto_assist_discovery(
    mpub: MqttPub,
    discovery_prefix: str,
    topic_prefix: str,
    ha_device_name: str,
    ha_device_id: str,
) -> None:
    if not mpub.client:
        return

    device_block = {
        "identifiers": [ha_device_id],
        "name": ha_device_name,
        "manufacturer": "tado°",
        "model": "Tado Assistant (Ingress)",
    }

    object_id = f"{ha_device_id}_auto_assist"
    config_topic = f"{discovery_prefix}/switch/{object_id}/config"

    payload = {
        "name": "Tado Assistant Auto-Assist",
        "unique_id": f"{ha_device_id}_auto_assist_switch",
        "state_topic": f"{topic_prefix}/{AUTO_ASSIST_STATE_TOPIC}",
        "command_topic": f"{topic_prefix}/{AUTO_ASSIST_SET_TOPIC}",
        "payload_on": "ON",
        "payload_off": "OFF",
        "state_on": "ON",
        "state_off": "OFF",
        "optimistic": False,
        "retain": True,
        "availability_topic": f"{topic_prefix}/_status",
        "payload_available": "online",
        "payload_not_available": "offline",
        "json_attributes_topic": f"{topic_prefix}/{AUTO_ASSIST_ATTRS_TOPIC}",
        "device": device_block,
        "icon": "mdi:robot",
    }

    mpub.publish_json(config_topic, payload, retain=True)


def publish_auto_assist_state(mpub: MqttPub, topic_prefix: str, st: Dict[str, Any]) -> None:
    if not mpub.client:
        return

    enabled = bool(st.get("enabled"))
    mpub.publish(f"{topic_prefix}/{AUTO_ASSIST_STATE_TOPIC}", "ON" if enabled else "OFF", retain=True)

    mpub.publish_json(
        f"{topic_prefix}/{AUTO_ASSIST_ATTRS_TOPIC}",
        {
            "enabled": enabled,
            "last_run": st.get("last_run"),
            "last_action": st.get("last_action"),
            "last_error": st.get("last_error"),
            "_ts": now_iso(),
        },
        retain=True,
    )


# -----------------------------
# Presence normalization + discovery
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


def discovery_object_ids(ha_device_id: str, device_id: int) -> Tuple[str, str]:
    # final object ids (stable)
    tracker_object_id = f"{ha_device_id}_presence_{device_id}"
    raw_object_id = f"{ha_device_id}_presence_{device_id}_raw"
    return tracker_object_id, raw_object_id


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
        home_object_id = f"{ha_device_id}_home_{home_id}_raw"
        home_config_topic = f"{discovery_prefix}/sensor/{home_object_id}/config"
        home_topic = f"{topic_prefix}/presence/home_{home_id}/raw"

        home_payload = {
            "name": f"Tado Home {home_id} (raw)",
            "unique_id": f"{ha_device_id}_home_{home_id}_raw",
            "state_topic": home_topic,
            "value_template": "{{ value_json._ts }}",
            "json_attributes_topic": home_topic,
            "availability_topic": availability_topic,
            "payload_available": "online",
            "payload_not_available": "offline",
            "device": device_block,
            "icon": "mdi:home-account",
        }
        mpub.publish_json(home_config_topic, home_payload, retain=True)

    for d in devices:
        did = d.get("id")
        if not did:
            continue
        did_int = int(did)
        name = d.get("name") or f"Device {did_int}"

        tracker_object_id, raw_object_id = discovery_object_ids(ha_device_id, did_int)

        state_topic = f"{topic_prefix}/presence/home_{home_id}/device_{did_int}/state"
        raw_topic = f"{topic_prefix}/presence/home_{home_id}/device_{did_int}/raw"

        # device_tracker
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
        home_topic = f"{topic_prefix}/presence/home_{home_id}/raw"
        mpub.publish_json(home_topic, {"_ts": now_iso(), "home_id": home_id, "devices": devices}, retain=True)

    cache = read_json(LAST_DEVICES_PATH)
    if not isinstance(cache, dict):
        cache = {}
    cache[str(home_id)] = {"_ts": now_iso(), "devices": devices}
    write_json_atomic(LAST_DEVICES_PATH, cache)


def republish_from_cache(mpub: MqttPub, topic_prefix: str, enable_raw: bool) -> None:
    cache = read_json(LAST_DEVICES_PATH)
    if not isinstance(cache, dict):
        return
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


# -----------------------------
# Legacy cleanup (remove old retained discovery configs)
# -----------------------------
def _cleanup_done() -> bool:
    m = read_json(CLEANUP_MARKER_PATH)
    return isinstance(m, dict) and m.get("done") is True


def _mark_cleanup_done() -> None:
    write_json_atomic(CLEANUP_MARKER_PATH, {"done": True, "ts": now_iso()})


def cleanup_legacy_discovery_once(
    mpub: MqttPub,
    discovery_prefix: str,
    candidate_ha_ids: List[str],
    home_ids: List[int],
    device_ids_by_home: Dict[int, Set[int]],
) -> None:
    if not mpub.client:
        return
    if _cleanup_done():
        return

    # Components we may have published historically
    components = ["binary_sensor", "device_tracker", "sensor", "switch"]

    # Old object id patterns we try to delete
    def object_id_variants(ha_id: str, home_id: int, dev_id: Optional[int]) -> List[str]:
        out: List[str] = []

        # switch
        out.append(f"{ha_id}_auto_assist")
        out.append(f"{ha_id}_tado_auto_assist")

        if dev_id is not None:
            out += [
                f"{ha_id}_presence_{dev_id}",
                f"{ha_id}_presence_{dev_id}_raw",
                f"{ha_id}_tado_presence_{dev_id}",
                f"{ha_id}_tado_presence_{dev_id}_raw",
                f"{ha_id}_presence_home_{home_id}_device_{dev_id}",
                f"{ha_id}_presence_home_{home_id}_device_{dev_id}_raw",
                f"{ha_id}_tado_presence_home_{home_id}_device_{dev_id}",
                f"{ha_id}_tado_presence_home_{home_id}_device_{dev_id}_raw",
            ]
        # home raw variants
        out += [
            f"{ha_id}_home_{home_id}_raw",
            f"{ha_id}_tado_presence_home_{home_id}",
            f"{ha_id}_tado_presence_home_{home_id}_raw",
        ]
        # de-dup preserve order
        seen: Set[str] = set()
        uniq: List[str] = []
        for x in out:
            if x not in seen:
                seen.add(x)
                uniq.append(x)
        return uniq

    deletions = 0

    for ha_id in candidate_ha_ids:
        for home_id in home_ids:
            # home-level objects
            for obj in object_id_variants(ha_id, home_id, None):
                for comp in components:
                    topic = f"{discovery_prefix}/{comp}/{obj}/config"
                    mpub.publish_delete_retained(topic)
                    deletions += 1

            for dev_id in sorted(list(device_ids_by_home.get(home_id, set()))):
                for obj in object_id_variants(ha_id, home_id, dev_id):
                    for comp in components:
                        topic = f"{discovery_prefix}/{comp}/{obj}/config"
                        mpub.publish_delete_retained(topic)
                        deletions += 1

    _mark_cleanup_done()
    log(f"legacy discovery cleanup done (deleted retained configs ~{deletions} topics)")


# -----------------------------
# Main
# -----------------------------
def main() -> None:
    cfg = load_config()

    poll = int(cfg["poll_seconds"])
    topic_prefix = cfg["topic_prefix"]
    discovery_prefix = cfg["discovery_prefix"]
    ha_device_name = cfg["ha_device_name"]
    ha_device_id = cfg["ha_device_id"]
    enable_raw_sensors = bool(cfg.get("enable_raw_sensors", True))
    cleanup_legacy = bool(cfg.get("cleanup_legacy_discovery", True))

    log(f"starting. poll_seconds={poll} enable_raw_sensors={enable_raw_sensors}")

    # Auto-Assist: OFF on boot
    aa_state = load_auto_assist_state(force_off_on_boot=True)
    save_auto_assist_state(aa_state)

    mpub = MqttPub(cfg["mqtt"])

    # Command handler
    def _on_mqtt_message(topic: str, payload: str) -> None:
        nonlocal aa_state
        if topic != f"{topic_prefix}/{AUTO_ASSIST_SET_TOPIC}":
            return

        cmd = (payload or "").strip().upper()
        if cmd not in ("ON", "OFF"):
            aa_state["last_run"] = now_iso()
            aa_state["last_action"] = "reject_command"
            aa_state["last_error"] = f"invalid payload: {payload!r}"
            save_auto_assist_state(aa_state)
            publish_auto_assist_state(mpub, topic_prefix, aa_state)
            return

        aa_state["enabled"] = (cmd == "ON")
        aa_state["last_run"] = now_iso()
        aa_state["last_action"] = "switch_on" if aa_state["enabled"] else "switch_off"
        aa_state["last_error"] = None
        save_auto_assist_state(aa_state)
        publish_auto_assist_state(mpub, topic_prefix, aa_state)
        log(f"Auto-Assist switched {cmd}")

    mpub.set_on_message(_on_mqtt_message)

    # Connect handler
    def _on_mqtt_connect() -> None:
        # Online status first (retained)
        mpub.publish(f"{topic_prefix}/_status", "online", retain=True)

        # Publish switch discovery + current state/attrs, then subscribe
        publish_auto_assist_discovery(mpub, discovery_prefix, topic_prefix, ha_device_name, ha_device_id)
        publish_auto_assist_state(mpub, topic_prefix, aa_state)
        mpub.subscribe(f"{topic_prefix}/{AUTO_ASSIST_SET_TOPIC}")
        log("auto_assist discovery/state published + subscribed")

        # One-time cleanup to remove duplicated entities (retained discovery configs from old versions)
        if cleanup_legacy:
            # Use any cached info to know device ids
            device_ids_by_home: Dict[int, Set[int]] = {}
            home_ids: List[int] = []
            cached = read_json(LAST_DEVICES_PATH)
            if isinstance(cached, dict):
                for hid_s, payload in cached.items():
                    try:
                        hid = int(hid_s)
                    except Exception:
                        continue
                    home_ids.append(hid)
                    device_ids_by_home.setdefault(hid, set())
                    if isinstance(payload, dict) and isinstance(payload.get("devices"), list):
                        for d in payload["devices"]:
                            try:
                                device_ids_by_home[hid].add(int(d.get("id")))
                            except Exception:
                                pass

            # If no cache, still try at least with configured home ids
            if not home_ids and isinstance(cfg.get("tado_home_ids"), list):
                home_ids = list(cfg["tado_home_ids"])

            # Candidate ha_device_ids we try to delete (covers old variants)
            candidates = [
                ha_device_id,
                "tado_assistant",
                "tado_assistant_tado",
                f"{ha_device_id}_tado",
            ]
            # De-dup
            seen = set()
            candidates = [x for x in candidates if x and (x not in seen and not seen.add(x))]  # type: ignore

            cleanup_legacy_discovery_once(
                mpub,
                discovery_prefix=discovery_prefix,
                candidate_ha_ids=candidates,
                home_ids=sorted(list(set(home_ids))),
                device_ids_by_home=device_ids_by_home,
            )

    mpub.set_on_connect(_on_mqtt_connect)

    # LWT so HA sees offline if container dies
    mpub.start(lwt_topic=f"{topic_prefix}/_status")

    # Republish cached presence early (helps HA show data during rate limit)
    republish_from_cache(mpub, topic_prefix, enable_raw_sensors)

    # Cache home ids to avoid /me every loop (reduces 429 together with official integration)
    cached_home_ids: Optional[List[int]] = cfg.get("tado_home_ids")
    if not cached_home_ids:
        st = read_json(DISCOVERY_STATE_PATH)
        if isinstance(st, dict) and isinstance(st.get("home_ids"), list):
            cached_home_ids = _parse_int_list(st.get("home_ids"))

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

            # Resolve home ids only if unknown
            if not cached_home_ids:
                cached_home_ids = get_home_ids(access_token)
                write_json_atomic(DISCOVERY_STATE_PATH, {"home_ids": cached_home_ids, "updated_at": now_iso()})
                log(f"home ids resolved: {cached_home_ids}")

            for home_id in cached_home_ids:
                devices_raw = get_mobile_devices(access_token, home_id)
                devices = [normalize_presence(d) for d in devices_raw]

                # Discovery (rare)
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
                    publish_auto_assist_discovery(mpub, discovery_prefix, topic_prefix, ha_device_name, ha_device_id)

                publish_presence(mpub, topic_prefix, home_id, devices, enable_raw_sensors)
                log(f"presence updated home={home_id} devices={len(devices)}")

            # Always publish Auto-Assist attrs so HA has attributes immediately
            aa_state = read_json(AUTO_ASSIST_STATE_PATH)
            if not isinstance(aa_state, dict):
                aa_state = {"enabled": False, "last_run": None, "last_action": None, "last_error": None}
            aa_state.setdefault("enabled", False)
            aa_state.setdefault("last_run", None)
            aa_state.setdefault("last_action", None)
            aa_state.setdefault("last_error", None)

            if aa_state.get("enabled") is True:
                aa_state["last_run"] = now_iso()
                aa_state["last_action"] = "tick"
                aa_state["last_error"] = None
                save_auto_assist_state(aa_state)

            publish_auto_assist_state(mpub, topic_prefix, aa_state)

            backoff_current = backoff_base
            time.sleep(poll)

        except RateLimitError as e:
            sleep_s = e.retry_after if e.retry_after is not None else backoff_current
            sleep_s = max(5, int(sleep_s))
            sleep_s = min(sleep_s, backoff_max)
            log(f"WARN: Tado rate limit (429) on {e.path} -> backoff {sleep_s}s")

            republish_from_cache(mpub, topic_prefix, enable_raw_sensors)

            backoff_current = min(backoff_current * 2, backoff_max)
            time.sleep(sleep_s)

        except Exception as e:
            # reflect error into auto-assist attrs
            try:
                aa = read_json(AUTO_ASSIST_STATE_PATH)
                if not isinstance(aa, dict):
                    aa = {"enabled": False}
                aa.setdefault("enabled", False)
                aa["last_run"] = now_iso()
                aa["last_action"] = "error"
                aa["last_error"] = str(e)
                save_auto_assist_state(aa)
                publish_auto_assist_state(mpub, topic_prefix, aa)
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
