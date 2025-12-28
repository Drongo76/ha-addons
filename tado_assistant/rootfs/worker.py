import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple, List, Callable

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
AUTO_ASSIST_STATE_PATH = os.path.join(DATA_DIR, "tado_assistant_auto_assist_state.json")

API_BASE = "https://my.tado.com/api/v2"

# OAuth refresh endpoint that works with refresh_token + client_id only
TOKEN_URL = "https://login.tado.com/oauth2/token"
TADO_CLIENT_ID = "1bb50063-6b0c-4d11-bd99-387f4a91cc46"

HTTP_TIMEOUT = 20
DEFAULT_POLL_SECONDS = 300

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


def load_config() -> Dict[str, Any]:
    opt = read_json(OPTIONS_PATH) or {}

    poll = opt.get("poll_seconds", opt.get("poll_interval", DEFAULT_POLL_SECONDS))
    try:
        poll = int(poll)
    except Exception:
        poll = DEFAULT_POLL_SECONDS
    poll = max(10, poll)

    mcfg = opt.get("mqtt", {})
    if not isinstance(mcfg, dict):
        mcfg = {}

    # flat keys compatibility
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
        "mqtt": mcfg,
        "topic_prefix": topic_prefix,
        "discovery_prefix": discovery_prefix,
        "ha_device_name": ha_device_name,
        "ha_device_id": ha_device_id,
        "raw": opt,
    }


# -----------------------------
# MQTT wrapper
# -----------------------------
class MqttPub:
    def __init__(self, cfg: Dict[str, Any]):
        self.cfg = cfg or {}
        self.client = None
        self._on_connect_cb: Optional[Callable[[], None]] = None
        self._on_message_cb: Optional[Callable[[str, str], None]] = None

    def set_on_connect(self, cb: Callable[[], None]) -> None:
        self._on_connect_cb = cb

    def set_on_message(self, cb: Callable[[str, str], None]) -> None:
        self._on_message_cb = cb

    def start(self, lwt_topic: Optional[str] = None) -> None:
        if mqtt is None:
            log("WARN: paho-mqtt not available, MQTT disabled")
            return

        if not bool(self.cfg.get("enabled", True)):
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
                self._on_connect_cb()

        def on_message(client, userdata, msg):
            try:
                t = msg.topic or ""
                p = msg.payload.decode("utf-8", errors="ignore") if msg.payload else ""
                if self._on_message_cb:
                    self._on_message_cb(t, p)
            except Exception:
                pass

        self.client.on_connect = on_connect
        self.client.on_message = on_message

        log(f"MQTT connecting to {host}:{port} (tls={use_tls}, user={'yes' if username else 'no'})")
        self.client.connect(host, port, keepalive=60)
        self.client.loop_start()

    def subscribe(self, topic: str) -> None:
        if self.client:
            self.client.subscribe(topic)

    def publish(self, topic: str, payload: str, retain: bool = True) -> None:
        if self.client:
            self.client.publish(topic, payload, qos=0, retain=retain)

    def publish_json(self, topic: str, payload: Any, retain: bool = True) -> None:
        self.publish(topic, json.dumps(payload, ensure_ascii=False), retain=retain)

    def publish_delete_retained(self, topic: str) -> None:
        self.publish(topic, "", retain=True)


# -----------------------------
# Tokens / refresh
# -----------------------------
def read_tokens() -> Dict[str, Any]:
    t = read_json(TOKENS_PATH)
    if not isinstance(t, dict):
        raise RuntimeError(f"tokens file invalid or missing: {TOKENS_PATH}")
    return t


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
    try:
        data = r.json()
    except Exception:
        data = r.text

    if r.status_code != 200 or not (isinstance(data, dict) and data.get("access_token")):
        raise RuntimeError(f"token refresh failed status={r.status_code} data={data}")

    new_tokens = dict(tokens)
    new_tokens.update(data)

    # keep refresh_token if not returned
    if not new_tokens.get("refresh_token"):
        new_tokens["refresh_token"] = refresh_token

    new_tokens["_obtained_at"] = now_iso()
    new_tokens["_obtained_at_epoch"] = int(time.time())

    write_json_atomic(TOKENS_PATH, new_tokens)
    log("Token refreshed.")
    return new_tokens


def api_get(path: str, access_token: str) -> Tuple[int, Any]:
    url = f"{API_BASE}{path}"
    r = requests.get(url, headers={"Authorization": f"Bearer {access_token}"}, timeout=HTTP_TIMEOUT)
    try:
        data = r.json()
    except Exception:
        data = r.text
    return r.status_code, data


def api_get_with_refresh(path: str, tokens: Dict[str, Any]) -> Tuple[Dict[str, Any], int, Any]:
    """GET with one refresh retry on 401."""
    access = tokens.get("access_token")
    if not access:
        raise RuntimeError("access_token missing in tokens file (login again via Ingress)")

    status, data = api_get(path, access)
    if status != 401:
        return tokens, status, data

    log(f"Tado API 401 on {path} -> refreshing token and retry")
    tokens = refresh_tokens(tokens)
    status, data = api_get(path, tokens["access_token"])
    return tokens, status, data


def get_home_ids(tokens: Dict[str, Any]) -> Tuple[Dict[str, Any], List[int]]:
    # IMPORTANT: Use /me. /homes list endpoint can return 403 for some accounts.
    tokens, status, data = api_get_with_refresh("/me", tokens)
    if status != 200:
        raise RuntimeError(f"Tado API {status} on /me: {data}")

    home_ids: List[int] = []
    if isinstance(data, dict):
        homes = data.get("homes")
        if isinstance(homes, list):
            for h in homes:
                if isinstance(h, dict) and isinstance(h.get("id"), int):
                    home_ids.append(int(h["id"]))
        if isinstance(data.get("homeId"), int):
            home_ids.append(int(data["homeId"]))

    home_ids = sorted(list(set(home_ids)))
    if not home_ids:
        raise RuntimeError(f"No home ids found in /me response: {data}")
    return tokens, home_ids


def get_mobile_devices(tokens: Dict[str, Any], home_id: int) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    path = f"/homes/{home_id}/mobileDevices"
    tokens, status, data = api_get_with_refresh(path, tokens)
    if status == 403:
        # This is the accessDenied you saw, but now it will be on the correct endpoint if it happens.
        raise RuntimeError(f"Tado API 403 on {path}: {data}")
    if status != 200:
        raise RuntimeError(f"Tado API {status} on {path}: {data}")

    if isinstance(data, list):
        return tokens, data
    return tokens, []


# -----------------------------
# Auto-Assist state + MQTT entity
# -----------------------------
def read_auto_assist_state() -> Dict[str, Any]:
    st = read_json(AUTO_ASSIST_STATE_PATH)
    if not isinstance(st, dict):
        st = {}
    st.setdefault("enabled", False)
    st.setdefault("last_run", None)
    st.setdefault("last_action", None)
    st.setdefault("last_error", None)
    return st


def boot_force_off() -> None:
    st = read_auto_assist_state()
    st["enabled"] = False
    st["last_action"] = "boot_off"
    st["last_error"] = None
    st["last_run"] = now_iso()
    write_json_atomic(AUTO_ASSIST_STATE_PATH, st)


def publish_auto_assist(mpub: MqttPub, cfg: Dict[str, Any], st: Dict[str, Any]) -> None:
    tp = cfg["topic_prefix"]
    mpub.publish(f"{tp}/{AUTO_ASSIST_STATE_TOPIC}", "ON" if st.get("enabled") else "OFF", retain=True)
    mpub.publish_json(
        f"{tp}/{AUTO_ASSIST_ATTRS_TOPIC}",
        {
            "enabled": bool(st.get("enabled")),
            "last_run": st.get("last_run"),
            "last_action": st.get("last_action"),
            "last_error": st.get("last_error"),
            "_ts": now_iso(),
        },
        retain=True,
    )


def cleanup_old_switch_discovery(mpub: MqttPub, cfg: Dict[str, Any]) -> None:
    dp = cfg["discovery_prefix"]
    ha_device_id = cfg["ha_device_id"]

    # old variants from earlier iterations
    for t in [
        f"{dp}/switch/{ha_device_id}_auto_assist/config",
        f"{dp}/switch/{ha_device_id}/{ha_device_id}_auto_assist/config",
        f"{dp}/switch/{ha_device_id}/{ha_device_id}_auto_assist_switch/config",
        f"{dp}/switch/{ha_device_id}_tado_assistant_auto_assist/config",
    ]:
        mpub.publish_delete_retained(t)


def publish_switch_discovery(mpub: MqttPub, cfg: Dict[str, Any]) -> None:
    dp = cfg["discovery_prefix"]
    tp = cfg["topic_prefix"]
    ha_device_id = cfg["ha_device_id"]
    ha_device_name = cfg["ha_device_name"]

    device_block = {
        "identifiers": [ha_device_id],
        "name": ha_device_name,
        "manufacturer": "tado°",
        "model": "Tado Assistant (Ingress)",
    }

    # canonical discovery topic
    node_id = ha_device_id
    object_id = "auto_assist"
    config_topic = f"{dp}/switch/{node_id}/{object_id}/config"

    # IMPORTANT: NO availability_topic -> avoids "not verfügbar" due to transient status
    payload = {
        "name": "Auto-Assist",
        "unique_id": f"{ha_device_id}_auto_assist",
        "state_topic": f"{tp}/{AUTO_ASSIST_STATE_TOPIC}",
        "command_topic": f"{tp}/{AUTO_ASSIST_SET_TOPIC}",
        "payload_on": "ON",
        "payload_off": "OFF",
        "optimistic": False,
        "retain": True,
        "json_attributes_topic": f"{tp}/{AUTO_ASSIST_ATTRS_TOPIC}",
        "device": device_block,
        "icon": "mdi:robot",
    }

    mpub.publish_json(config_topic, payload, retain=True)
    log(f"auto_assist discovery published: {config_topic}")


# -----------------------------
# Main
# -----------------------------
def main() -> None:
    cfg = load_config()
    poll = int(cfg["poll_seconds"])
    tp = cfg["topic_prefix"]

    log(f"starting. poll_seconds={poll}")

    boot_force_off()

    mpub = MqttPub(cfg["mqtt"])

    def on_message(topic: str, payload: str) -> None:
        wanted = f"{tp}/{AUTO_ASSIST_SET_TOPIC}"
        if topic != wanted:
            return
        cmd = (payload or "").strip().upper()
        st = read_auto_assist_state()

        if cmd not in ("ON", "OFF"):
            st["last_error"] = f"invalid command payload: '{payload}'"
            st["last_action"] = "reject_command"
            st["last_run"] = now_iso()
            write_json_atomic(AUTO_ASSIST_STATE_PATH, st)
            publish_auto_assist(mpub, cfg, st)
            log(f"auto_assist command rejected payload='{payload}'")
            return

        st["enabled"] = (cmd == "ON")
        st["last_action"] = "switch_on" if st["enabled"] else "switch_off"
        st["last_error"] = None
        st["last_run"] = now_iso()
        write_json_atomic(AUTO_ASSIST_STATE_PATH, st)
        publish_auto_assist(mpub, cfg, st)
        log(f"auto_assist command received: {cmd}")

    mpub.set_on_message(on_message)

    def on_connect() -> None:
        # publish status only after connect
        mpub.publish(f"{tp}/_status", "online", retain=True)
        log(f"status published: {tp}/_status = online")

        cleanup_old_switch_discovery(mpub, cfg)
        publish_switch_discovery(mpub, cfg)

        # initial state + attrs
        publish_auto_assist(mpub, cfg, read_auto_assist_state())

        # subscribe to commands
        mpub.subscribe(f"{tp}/{AUTO_ASSIST_SET_TOPIC}")
        log(f"auto_assist subscribed: {tp}/{AUTO_ASSIST_SET_TOPIC}")

    mpub.set_on_connect(on_connect)

    # LWT sets offline if addon dies (still helpful for presence entities etc.)
    mpub.start(lwt_topic=f"{tp}/_status")

    # ---- Tado polling loop ----
    home_ids: Optional[List[int]] = None
    tokens: Optional[Dict[str, Any]] = None

    while True:
        try:
            tokens = read_tokens()
            if home_ids is None:
                tokens, home_ids = get_home_ids(tokens)
                log(f"home ids resolved: {home_ids}")

            # just prove API access; your presence publishing is elsewhere in your repo
            for hid in home_ids:
                tokens, devices = get_mobile_devices(tokens, hid)
                log(f"mobileDevices ok: home={hid} devices={len(devices)}")

            # heartbeat attrs when enabled
            st = read_auto_assist_state()
            if st.get("enabled") is True:
                st["last_run"] = now_iso()
                st["last_action"] = "tick"
                st["last_error"] = None
                write_json_atomic(AUTO_ASSIST_STATE_PATH, st)
                publish_auto_assist(mpub, cfg, st)

            time.sleep(poll)

        except Exception as e:
            st = read_auto_assist_state()
            st["last_run"] = now_iso()
            st["last_action"] = "error"
            st["last_error"] = str(e)
            write_json_atomic(AUTO_ASSIST_STATE_PATH, st)
            publish_auto_assist(mpub, cfg, st)
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
