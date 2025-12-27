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
    mqtt = None


# ===== Paths =====
DATA_DIR = "/data"
TOKENS_PATH = os.path.join(DATA_DIR, "tado_assistant_tokens.json")
LAST_TOKEN_RESP_PATH = os.path.join(DATA_DIR, "tado_assistant_last_token_response.json")
OPTIONS_PATH = os.path.join(DATA_DIR, "options.json")

# Merkt sich, welche Device-IDs wir zuletzt per Discovery veröffentlicht haben (pro home)
DISCOVERY_STATE_PATH = os.path.join(DATA_DIR, "tado_assistant_discovery_state.json")

# ===== Tado OAuth + API =====
TADO_CLIENT_ID = "1bb50063-6b0c-4d11-bd99-387f4a91cc46"
TOKEN_URL = "https://login.tado.com/oauth2/token"
API_BASE = "https://my.tado.com/api/v2"

# ===== Defaults =====
DEFAULT_POLL_SECONDS = 30
TOKEN_REFRESH_SAFETY_SECONDS = 90
HTTP_TIMEOUT = 20

# Discovery republish (falls HA/retained mal „verschluckt“ wurde)
DISCOVERY_REPUBLISH_EVERY_LOOPS = 20


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


def load_config() -> Dict[str, Any]:
    opt = read_json(OPTIONS_PATH) or {}

    poll = opt.get("poll_seconds", opt.get("poll_interval", DEFAULT_POLL_SECONDS))
    try:
        poll = int(poll)
    except Exception:
        poll = DEFAULT_POLL_SECONDS

    mcfg = opt.get("mqtt", {})
    if not isinstance(mcfg, dict):
        mcfg = {}

    # flache HA UI keys
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

    def publish(self, topic: str, payload: str, retain: bool = True) -> None:
        if not self.client:
            return
        self.client.publish(topic, payload, qos=0, retain=retain)

    def publish_json(self, topic: str, payload: Dict[str, Any], retain: bool = True) -> None:
        self.publish(topic, json.dumps(payload, ensure_ascii=False), retain=retain)

    def publish_delete_retained(self, topic: str) -> None:
        # retained löschen: leere Payload retained (HA Discovery: config löschen)
        self.publish(topic, "", retain=True)


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
        raise RuntimeError(f"Tokens missing/invalid at {TOKENS_PATH}")

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

    return {"id": dev_id, "name": name, "state": state, "at_home": at_home, "_ts": now_iso()}


def discovery_object_ids(ha_device_id: str, device_id: int) -> Tuple[str, str]:
    # object_id muss stabil bleiben, sonst entstehen Duplikate
    bin_object_id = f"{ha_device_id}_presence_{device_id}"
    json_object_id = f"{ha_device_id}_presence_{device_id}_json"
    return bin_object_id, json_object_id


def publish_discovery_for_devices(
    mpub: MqttPub,
    discovery_prefix: str,
    topic_prefix: str,
    ha_device_name: str,
    ha_device_id: str,
    home_id: int,
    devices: list,
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

    for d in devices:
        did = d.get("id")
        name = d.get("name") or f"Device {did}"
        if not did:
            continue

        bin_object_id, json_object_id = discovery_object_ids(ha_device_id, int(did))

        state_topic = f"{topic_prefix}/presence/home_{home_id}/device_{did}/state"
        json_attr_topic = f"{topic_prefix}/presence/home_{home_id}/device_{did}/json"

        # Binary sensor
        bin_config_topic = f"{discovery_prefix}/binary_sensor/{bin_object_id}/config"
        bin_payload = {
            "name": f"Tado Presence {name}",
            "unique_id": f"{ha_device_id}_home_{home_id}_device_{did}_presence",
            "state_topic": state_topic,
            "payload_on": "home",
            "payload_off": "away",
            "device_class": "presence",
            "availability_topic": availability_topic,
            "payload_available": "online",
            "payload_not_available": "offline",
            "device": device_block,
        }
        mpub.publish_json(bin_config_topic, bin_payload, retain=True)

        # JSON sensor (attributes)
        json_config_topic = f"{discovery_prefix}/sensor/{json_object_id}/config"
        json_payload = {
            "name": f"Tado Presence {name} (raw)",
            "unique_id": f"{ha_device_id}_home_{home_id}_device_{did}_json",
            "state_topic": state_topic,
            "json_attributes_topic": json_attr_topic,
            "availability_topic": availability_topic,
            "payload_available": "online",
            "payload_not_available": "offline",
            "device": device_block,
            "icon": "mdi:account",
        }
        mpub.publish_json(json_config_topic, json_payload, retain=True)

    # aggregated home sensor
    agg_object_id = f"{ha_device_id}_home_{home_id}_presence"
    agg_config_topic = f"{discovery_prefix}/sensor/{agg_object_id}/config"
    agg_topic = f"{topic_prefix}/presence/home_{home_id}"
    agg_payload = {
        "name": f"Tado Presence Home {home_id}",
        "unique_id": f"{ha_device_id}_home_{home_id}_presence_json",
        "state_topic": agg_topic,
        "value_template": "{{ value_json._ts }}",
        "json_attributes_topic": agg_topic,
        "availability_topic": availability_topic,
        "payload_available": "online",
        "payload_not_available": "offline",
        "device": device_block,
        "icon": "mdi:home-account",
    }
    mpub.publish_json(agg_config_topic, agg_payload, retain=True)


def discovery_cleanup_removed_devices(
    mpub: MqttPub,
    discovery_prefix: str,
    ha_device_id: str,
    removed_device_ids: set,
) -> None:
    if not mpub.client:
        return
    for did in sorted(list(removed_device_ids)):
        bin_object_id, json_object_id = discovery_object_ids(ha_device_id, int(did))
        bin_config_topic = f"{discovery_prefix}/binary_sensor/{bin_object_id}/config"
        json_config_topic = f"{discovery_prefix}/sensor/{json_object_id}/config"
        mpub.publish_delete_retained(bin_config_topic)
        mpub.publish_delete_retained(json_config_topic)
        log(f"discovery cleanup: removed device_id={did}")


def load_discovery_state() -> Dict[str, Any]:
    st = read_json(DISCOVERY_STATE_PATH)
    if not isinstance(st, dict):
        return {"homes": {}}
    st.setdefault("homes", {})
    if not isinstance(st["homes"], dict):
        st["homes"] = {}
    return st


def save_discovery_state(st: Dict[str, Any]) -> None:
    write_json_atomic(DISCOVERY_STATE_PATH, st)


def main() -> None:
    cfg = load_config()
    poll = cfg["poll_seconds"]
    topic_prefix = cfg["topic_prefix"]
    discovery_prefix = cfg["discovery_prefix"]
    ha_device_name = cfg["ha_device_name"]
    ha_device_id = cfg["ha_device_id"]

    log(f"starting. poll_seconds={poll}")

    mpub = MqttPub(cfg["mqtt"])
    mpub.start()

    # availability online
    if mpub.client:
        mpub.publish(f"{topic_prefix}/_status", "online", retain=True)
        log(f"status published: {topic_prefix}/_status = online")

    discovery_state = load_discovery_state()
    loop = 0

    # WICHTIG: Erste Runde läuft sofort (ohne Sleep), damit HA nicht lange "unknown" bleibt
    while True:
        loop += 1
        try:
            if not tokens_exist():
                log(f"waiting for tokens: {TOKENS_PATH} (login first)")
                time.sleep(5)
                continue

            tokens = ensure_valid_tokens()
            access_token = tokens["access_token"]

            home_ids = get_home_ids(access_token)

            for home_id in home_ids:
                devices_raw = get_mobile_devices(access_token, home_id)
                devices = [normalize_presence(d) for d in devices_raw]

                # 1) ZUERST state/json publishen (retained), dann Discovery (damit HA direkt Werte hat)
                agg_topic = f"{topic_prefix}/presence/home_{home_id}"
                mpub.publish_json(
                    agg_topic,
                    {"home_id": home_id, "devices": devices, "_ts": now_iso()},
                    retain=True,
                )

                for d in devices:
                    did = d.get("id")
                    if not did:
                        continue
                    base = f"{topic_prefix}/presence/home_{home_id}/device_{did}"
                    mpub.publish_json(base + "/json", d, retain=True)
                    mpub.publish(base + "/state", d["state"], retain=True)

                # 2) Discovery + Cleanup
                current_ids = {int(d["id"]) for d in devices if d.get("id")}
                prev_ids = set(discovery_state["homes"].get(str(home_id), {}).get("device_ids", []))
                removed = prev_ids - current_ids

                if mpub.client:
                    if removed:
                        discovery_cleanup_removed_devices(mpub, discovery_prefix, ha_device_id, removed)

                    if loop == 1 or loop % DISCOVERY_REPUBLISH_EVERY_LOOPS == 0:
                        publish_discovery_for_devices(
                            mpub,
                            discovery_prefix,
                            topic_prefix,
                            ha_device_name,
                            ha_device_id,
                            home_id,
                            devices,
                        )
                        log(f"discovery published (home={home_id}, devices={len(current_ids)})")

                    discovery_state["homes"][str(home_id)] = {
                        "device_ids": sorted(list(current_ids)),
                        "updated_at": now_iso(),
                    }
                    save_discovery_state(discovery_state)

                log(f"presence updated home={home_id} devices={len(devices)}")

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
