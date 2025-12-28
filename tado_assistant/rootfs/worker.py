import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

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

# Cache für Home-IDs (damit wir /me NICHT ständig aufrufen -> 429)
HOME_IDS_CACHE_PATH = os.path.join(DATA_DIR, "tado_assistant_home_ids_cache.json")

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

# Home-IDs Cache TTL (sek). Wenn abgelaufen, versuchen wir EINMAL /me – aber nutzen alten Cache als Fallback.
HOME_IDS_CACHE_TTL_SECONDS = 7 * 24 * 3600  # 7 Tage


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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


def _parse_int_list(val: Any) -> List[int]:
    """
    Akzeptiert:
      - int
      - list[int|str]
      - str: "1,2,3"
    """
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
    if "mqtt_client_id" in opt:
        mcfg["client_id"] = opt.get("mqtt_client_id")

    topic_prefix = opt.get("mqtt_topic_prefix") or opt.get("topic_prefix") or "tado_assistant"
    topic_prefix = str(topic_prefix).strip().strip("/")

    discovery_prefix = opt.get("mqtt_discovery_prefix") or opt.get("discovery_prefix") or "homeassistant"
    discovery_prefix = str(discovery_prefix).strip().strip("/")

    ha_device_name = opt.get("ha_device_name") or "Tado Assistant"
    ha_device_id = opt.get("ha_device_id") or "tado_assistant"

    # Optional: Home IDs fix/override, damit wir /me gar nicht brauchen.
    # Unterstützte Keys: tado_home_id, tado_home_ids, home_ids
    override = (
        opt.get("tado_home_ids")
        if "tado_home_ids" in opt
        else opt.get("home_ids")
        if "home_ids" in opt
        else opt.get("tado_home_id")
    )
    home_ids_override = sorted(list(set(_parse_int_list(override))))

    enable_raw_sensors = opt.get("enable_raw_sensors")
    if enable_raw_sensors is None:
        enable_raw_sensors = True
    else:
        enable_raw_sensors = bool(enable_raw_sensors)

    return {
        "poll_seconds": poll,
        "mqtt": mcfg,
        "topic_prefix": topic_prefix,
        "discovery_prefix": discovery_prefix,
        "ha_device_name": ha_device_name,
        "ha_device_id": ha_device_id,
        "home_ids_override": home_ids_override,
        "enable_raw_sensors": enable_raw_sensors,
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

    def publish_json(self, topic: str, payload: Any, retain: bool = True) -> None:
        self.publish(topic, json.dumps(payload, ensure_ascii=False), retain=retain)

    def publish_delete_retained(self, topic: str) -> None:
        self.publish(topic, "", retain=True)


def api_request(method: str, path: str, access_token: str, params: Optional[dict] = None) -> Tuple[int, Any]:
    url = f"{API_BASE}{path}"
    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        r = requests.request(method, url, headers=headers, params=params, timeout=HTTP_TIMEOUT)
    except Exception as e:
        return 0, f"request failed: {e}"
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


def _extract_home_ids_from_me(data: Any) -> List[int]:
    home_ids: List[int] = []
    if isinstance(data, dict):
        if isinstance(data.get("homes"), list):
            for h in data["homes"]:
                if isinstance(h, dict):
                    hid = h.get("id") or h.get("homeId")
                    if isinstance(hid, int):
                        home_ids.append(hid)
        if isinstance(data.get("homeId"), int):
            home_ids.append(data["homeId"])
    return sorted(list(set([int(x) for x in home_ids if isinstance(x, int)])))


def _read_home_ids_cache() -> Tuple[Optional[List[int]], Optional[int]]:
    obj = read_json(HOME_IDS_CACHE_PATH)
    if not isinstance(obj, dict):
        return None, None
    ids = obj.get("home_ids")
    cached_at = obj.get("cached_at_epoch")
    if not isinstance(ids, list):
        return None, None
    try:
        ids_int = sorted(list(set([int(x) for x in ids])))
    except Exception:
        return None, None
    try:
        cached_at_int = int(cached_at) if cached_at is not None else None
    except Exception:
        cached_at_int = None
    return ids_int, cached_at_int


def _write_home_ids_cache(home_ids: List[int]) -> None:
    write_json_atomic(
        HOME_IDS_CACHE_PATH,
        {"home_ids": sorted(list(set([int(x) for x in home_ids]))), "cached_at_epoch": int(time.time()), "_at": now_iso()},
    )


def resolve_home_ids(access_token: str, discovery_state: Dict[str, Any], cfg: Dict[str, Any]) -> List[int]:
    # 0) Override aus options.json
    override = cfg.get("home_ids_override") or []
    if override:
        _write_home_ids_cache(override)
        log(f"home ids: using override from options.json: {override}")
        return override

    # 1) Cache
    cached_ids, cached_at = _read_home_ids_cache()
    cache_age = (int(time.time()) - cached_at) if cached_ids and cached_at else None
    if cached_ids and cache_age is not None and cache_age <= HOME_IDS_CACHE_TTL_SECONDS:
        return cached_ids

    # 2) Discovery-State
    try:
        homes = discovery_state.get("homes")
        if isinstance(homes, dict) and homes:
            ids = []
            for k in homes.keys():
                try:
                    ids.append(int(k))
                except Exception:
                    continue
            ids = sorted(list(set(ids)))
            if ids:
                _write_home_ids_cache(ids)
                log(f"home ids: using from discovery_state: {ids}")
                return ids
    except Exception:
        pass

    # 3) /me (nur wenn wir GAR nichts haben)
    status, data = api_request("GET", "/me", access_token)
    if status == 200:
        ids = _extract_home_ids_from_me(data)
        if not ids:
            raise RuntimeError(f"No home ids found in /me response: {data}")
        _write_home_ids_cache(ids)
        return ids

    # 4) Fallback: alter Cache auch wenn TTL abgelaufen ist
    if cached_ids:
        log(f"WARN: /me failed status={status}. Using stale cache home_ids={cached_ids}")
        return cached_ids

    raise RuntimeError(f"/me failed status={status} data={data}")


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

    # HA device_tracker: "home" / "not_home"
    if isinstance(at_home, bool):
        state = "home" if at_home else "not_home"
    else:
        state = "unknown"

    return {
        "id": dev_id,
        "name": name,
        "state": state,
        "at_home": at_home,
        "_ts": now_iso(),
        "raw": device,
    }


def discovery_object_ids(ha_device_id: str, device_id: int) -> Tuple[str, str]:
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
    devices: list,
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

    for d in devices:
        did = d.get("id")
        name = d.get("name") or f"Device {did}"
        if not did:
            continue
        did_int = int(did)

        tracker_object_id, raw_object_id = discovery_object_ids(ha_device_id, did_int)

        state_topic = f"{topic_prefix}/presence/home_{home_id}/device_{did}/state"
        json_attr_topic = f"{topic_prefix}/presence/home_{home_id}/device_{did}/json"

        # Device Tracker (statt binary_sensor)
        tracker_config_topic = f"{discovery_prefix}/device_tracker/{tracker_object_id}/config"
        tracker_payload = {
            "name": f"Tado Presence {name}",
            "unique_id": f"{ha_device_id}_home_{home_id}_device_{did}_tracker",
            "state_topic": state_topic,
            "payload_home": "home",
            "payload_not_home": "not_home",
            "source_type": "gps",
            "json_attributes_topic": json_attr_topic,
            "availability_topic": availability_topic,
            "payload_available": "online",
            "payload_not_available": "offline",
            "device": device_block,
        }
        mpub.publish_json(tracker_config_topic, tracker_payload, retain=True)

        if enable_raw_sensors:
            raw_config_topic = f"{discovery_prefix}/sensor/{raw_object_id}/config"
            raw_payload = {
                "name": f"Tado Presence {name} (raw)",
                "unique_id": f"{ha_device_id}_home_{home_id}_device_{did}_raw",
                "state_topic": state_topic,
                "json_attributes_topic": json_attr_topic,
                "availability_topic": availability_topic,
                "payload_available": "online",
                "payload_not_available": "offline",
                "device": device_block,
                "icon": "mdi:account",
            }
            mpub.publish_json(raw_config_topic, raw_payload, retain=True)

    if enable_raw_sensors:
        agg_object_id = f"{ha_device_id}_home_{home_id}_raw"
        agg_config_topic = f"{discovery_prefix}/sensor/{agg_object_id}/config"
        agg_topic = f"{topic_prefix}/presence/home_{home_id}"
        agg_payload = {
            "name": f"Tado Presence Home {home_id} (raw)",
            "unique_id": f"{ha_device_id}_home_{home_id}_raw",
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
    enable_raw_sensors: bool,
) -> None:
    if not mpub.client:
        return
    for did in sorted(list(removed_device_ids)):
        tracker_object_id, raw_object_id = discovery_object_ids(ha_device_id, int(did))

        tracker_config_topic = f"{discovery_prefix}/device_tracker/{tracker_object_id}/config"
        mpub.publish_delete_retained(tracker_config_topic)

        if enable_raw_sensors:
            raw_config_topic = f"{discovery_prefix}/sensor/{raw_object_id}/config"
            mpub.publish_delete_retained(raw_config_topic)

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
    enable_raw_sensors = bool(cfg.get("enable_raw_sensors", True))

    log(f"starting. poll_seconds={poll} enable_raw_sensors={enable_raw_sensors}")

    mpub = MqttPub(cfg["mqtt"])
    mpub.start()

    if mpub.client:
        mpub.publish(f"{topic_prefix}/_status", "online", retain=True)
        log(f"status published: {topic_prefix}/_status = online")

    discovery_state = load_discovery_state()
    loop = 0
    home_ids: Optional[List[int]] = None

    while True:
        loop += 1
        try:
            if not tokens_exist():
                log(f"waiting for tokens: {TOKENS_PATH} (login first)")
                time.sleep(5)
                continue

            tokens = ensure_valid_tokens()
            access_token = tokens["access_token"]

            # Home-IDs NICHT jedes Mal holen! (das verursacht 429)
            if home_ids is None:
                home_ids = resolve_home_ids(access_token, discovery_state, cfg)
                log(f"home ids resolved: {home_ids}")

            for home_id in home_ids:
                devices_raw = get_mobile_devices(access_token, home_id)
                devices = [normalize_presence(d) for d in devices_raw]

                agg_topic = f"{topic_prefix}/presence/home_{home_id}"
                agg_devices_slim = [
                    {"id": d.get("id"), "name": d.get("name"), "state": d.get("state"), "at_home": d.get("at_home"), "_ts": d.get("_ts")}
                    for d in devices
                ]
                mpub.publish_json(
                    agg_topic,
                    {"home_id": home_id, "devices": agg_devices_slim, "_ts": now_iso()},
                    retain=True,
                )

                for d in devices:
                    did = d.get("id")
                    if not did:
                        continue
                    base = f"{topic_prefix}/presence/home_{home_id}/device_{did}"
                    mpub.publish_json(base + "/json", d, retain=True)
                    mpub.publish(base + "/state", str(d.get("state") or "unknown"), retain=True)

                current_ids = {int(d["id"]) for d in devices if d.get("id")}
                prev_ids = set(discovery_state["homes"].get(str(home_id), {}).get("device_ids", []))
                removed = prev_ids - current_ids

                if mpub.client:
                    if removed:
                        discovery_cleanup_removed_devices(
                            mpub, discovery_prefix, ha_device_id, removed, enable_raw_sensors=enable_raw_sensors
                        )

                    if loop == 1 or loop % DISCOVERY_REPUBLISH_EVERY_LOOPS == 0:
                        publish_discovery_for_devices(
                            mpub,
                            discovery_prefix,
                            topic_prefix,
                            ha_device_name,
                            ha_device_id,
                            home_id,
                            devices,
                            enable_raw_sensors=enable_raw_sensors,
                        )
                        log(f"discovery published (home={home_id}, devices={len(current_ids)})")

                    discovery_state["homes"][str(home_id)] = {
                        "device_ids": sorted(list(current_ids)),
                        "updated_at": now_iso(),
                    }
                    save_discovery_state(discovery_state)

                log(f"presence updated home={home_id} devices={len(devices)}")

        except Exception as e:
            msg = str(e)
            log(f"ERROR: {msg}")
            if home_ids is None and "status=429" in msg:
                time.sleep(60)

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
