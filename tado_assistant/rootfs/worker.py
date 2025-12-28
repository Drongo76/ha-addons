#!/usr/bin/env python3
import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple, List, Set

import requests

try:
    import paho.mqtt.client as mqtt
except Exception:
    mqtt = None


# ===== Paths =====
DATA_DIR = "/data"
TOKENS_PATH = os.path.join(DATA_DIR, "tado_assistant_tokens.json")
LAST_TOKEN_RESP_PATH = os.path.join(DATA_DIR, "tado_assistant_last_token_response.json")

# Auto-Assist runtime/state (separate from tokens!)
ASSIST_STATE_PATH = os.path.join(DATA_DIR, "tado_assistant_state.json")

# ===== Defaults =====
DEFAULT_TOPIC_PREFIX = "tado_assistant"
DEFAULT_DISCOVERY_PREFIX = "homeassistant"
DEFAULT_DEVICE_NAME = "Tado Assistant"
DEFAULT_DEVICE_ID = "tado_assistant"
DEFAULT_POLL_SECONDS = 300  # IMPORTANT: avoid 429 by default


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
        log(f"ERROR reading {path}: {e}")
        return None


def write_json_atomic(path: str, obj: Dict[str, Any]) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def load_config() -> Dict[str, Any]:
    opts = read_json("/data/options.json") or {}

    topic_prefix = (opts.get("topic_prefix") or DEFAULT_TOPIC_PREFIX).strip().strip("/")
    discovery_prefix = (opts.get("discovery_prefix") or DEFAULT_DISCOVERY_PREFIX).strip().strip("/")

    ha_device_name = (opts.get("ha_device_name") or DEFAULT_DEVICE_NAME).strip()
    ha_device_id = (opts.get("ha_device_id") or DEFAULT_DEVICE_ID).strip().strip("/")

    # Polling (default 300s!)
    poll_seconds = int(opts.get("poll_seconds") or DEFAULT_POLL_SECONDS)
    if poll_seconds < 30:
        poll_seconds = 30

    enable_raw_sensors = bool(opts.get("enable_raw_sensors", True))
    # Keep legacy aggregate raw sensor (home_<id>_raw) to have "7 entities like before"
    enable_home_raw_sensor = bool(opts.get("enable_home_raw_sensor", True))

    # Debug endpoint flags (if you have a /debug route elsewhere; worker only respects config for publishing extra logs)
    debug_enabled = bool(opts.get("debug_enabled", False))

    mqtt_cfg = {
        "enabled": bool(opts.get("mqtt_enabled", True)),
        "host": opts.get("mqtt_host") or "core-mosquitto",
        "port": int(opts.get("mqtt_port") or 1883),
        "username": opts.get("mqtt_username") or "",
        "password": opts.get("mqtt_password") or "",
        "tls": bool(opts.get("mqtt_tls", False)),
        "client_id": opts.get("mqtt_client_id") or "tado-assistant",
    }

    return {
        "topic_prefix": topic_prefix,
        "discovery_prefix": discovery_prefix,
        "ha_device_name": ha_device_name,
        "ha_device_id": ha_device_id,
        "poll_seconds": poll_seconds,
        "enable_raw_sensors": enable_raw_sensors,
        "enable_home_raw_sensor": enable_home_raw_sensor,
        "debug_enabled": debug_enabled,
        "mqtt": mqtt_cfg,
    }


# ===== Token helpers =====
def tokens_exist() -> bool:
    t = read_json(TOKENS_PATH)
    return bool(t and t.get("access_token"))


def parse_obtained_at(tokens: Dict[str, Any]) -> Optional[float]:
    # stored by our ingress login
    if isinstance(tokens.get("obtained_at"), (int, float)):
        return float(tokens["obtained_at"])
    # fallback: iso timestamp
    s = tokens.get("obtained_at_iso")
    if isinstance(s, str):
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
        except Exception:
            return None
    return None


def token_expires_at(tokens: Dict[str, Any]) -> Optional[float]:
    obtained = parse_obtained_at(tokens)
    if obtained is None:
        return None
    exp = tokens.get("expires_in")
    try:
        exp_s = int(exp)
    except Exception:
        return None
    return obtained + exp_s


def token_needs_refresh(tokens: Dict[str, Any], skew_s: int = 60) -> bool:
    exp_at = token_expires_at(tokens)
    if exp_at is None:
        # can't determine; assume refresh if we have refresh_token & client creds
        return True
    return (time.time() + skew_s) >= exp_at


def refresh_tokens(tokens: Dict[str, Any]) -> Dict[str, Any]:
    # Device-Code flow gave us refresh_token + client_id/client_secret (needed for refresh)
    refresh_token = tokens.get("refresh_token")
    client_id = tokens.get("client_id")
    client_secret = tokens.get("client_secret")

    if not (refresh_token and client_id and client_secret):
        raise RuntimeError("refresh_token/client_id/client_secret missing in tokens file")

    url = "https://auth.tado.com/oauth/token"
    data = {
        "grant_type": "refresh_token",
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
    }

    r = requests.post(url, data=data, timeout=20)
    if r.status_code >= 400:
        raise RuntimeError(f"token refresh failed: {r.status_code} {r.text[:200]}")

    resp = r.json()
    # Persist last response for debug
    try:
        write_json_atomic(LAST_TOKEN_RESP_PATH, resp)
    except Exception:
        pass

    # Merge fields, keep refresh_token if not returned
    new_tokens = dict(tokens)
    new_tokens.update(resp)
    if not new_tokens.get("refresh_token"):
        new_tokens["refresh_token"] = refresh_token

    # Set obtained_at
    new_tokens["obtained_at"] = time.time()
    new_tokens["obtained_at_iso"] = now_iso()

    write_json_atomic(TOKENS_PATH, new_tokens)
    return new_tokens


def api_request(tokens: Dict[str, Any], method: str, path: str) -> Any:
    base = "https://my.tado.com/api/v2"
    url = base + path
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}

    r = requests.request(method, url, headers=headers, timeout=30)
    if r.status_code == 429:
        # caller handles backoff
        raise requests.HTTPError("429", response=r)
    if r.status_code >= 400:
        raise RuntimeError(f"Tado API {r.status_code} on {path}: {r.text[:200]}")
    return r.json()


def ensure_valid_tokens() -> Dict[str, Any]:
    tokens = read_json(TOKENS_PATH) or {}
    if not tokens.get("access_token"):
        raise RuntimeError("tokens missing access_token (login again)")

    # Refresh if needed
    if token_needs_refresh(tokens):
        log("token needs refresh -> refreshing")
        tokens = refresh_tokens(tokens)
        log("token refreshed")
    return tokens


def get_home_ids(tokens: Dict[str, Any]) -> List[int]:
    homes = api_request(tokens, "GET", "/homes")
    ids: List[int] = []
    for h in homes or []:
        hid = h.get("id")
        if isinstance(hid, int):
            ids.append(hid)
    return ids


def get_mobile_devices(tokens: Dict[str, Any], home_id: int) -> List[Dict[str, Any]]:
    return api_request(tokens, "GET", f"/homes/{home_id}/mobileDevices") or []


def normalize_presence(device: Dict[str, Any]) -> Dict[str, Any]:
    # Normalize to: id, name, state(home/not_home), raw JSON, last_seen
    did = device.get("id")
    name = device.get("name") or device.get("deviceName") or device.get("model") or f"device_{did}"
    settings = device.get("settings") or {}
    geo = settings.get("geoTrackingEnabled")
    # tado mobileDevices has "location" or "atHome" depending on account; we normalize robustly:
    at_home = device.get("atHome")
    if at_home is None:
        # some payloads: "location": {"atHome": true}
        loc = device.get("location") or {}
        at_home = loc.get("atHome")
    state = "home" if at_home else "not_home"

    return {
        "id": did,
        "name": name,
        "state": state,
        "geoTrackingEnabled": geo,
        "raw": device,
        "_ts": now_iso(),
    }


def discovery_object_ids(device_name: str) -> Tuple[str, str]:
    # Return (object_id, safe_id) for discovery
    # object_id used in entity_id; keep stable-ish
    s = device_name.lower().strip()
    out = []
    for ch in s:
        if ch.isalnum():
            out.append(ch)
        else:
            out.append("_")
    base = "".join(out)
    while "__" in base:
        base = base.replace("__", "_")
    base = base.strip("_") or "device"
    return base, base


class MqttPub:
    def __init__(self, mqtt_cfg: Dict[str, Any]) -> None:
        self.cfg = mqtt_cfg or {}
        self.client = None  # type: ignore

    def start(self, will_topic: Optional[str] = None) -> None:
        enabled = bool(self.cfg.get("enabled", False))
        if not enabled:
            log("MQTT disabled (options.json mqtt_enabled=false)")
            return
        if mqtt is None:
            log("ERROR: paho-mqtt not installed, but mqtt enabled")
            return

        host = self.cfg.get("host") or "core-mosquitto"
        port = int(self.cfg.get("port") or 1883)
        username = self.cfg.get("username") or None
        password = self.cfg.get("password") or None
        tls = bool(self.cfg.get("tls", False))
        client_id = self.cfg.get("client_id") or "tado-assistant"

        c = mqtt.Client(client_id=client_id, clean_session=True)
        if username:
            c.username_pw_set(username, password or "")

        if tls:
            c.tls_set()

        if will_topic:
            # LWT retained -> HA can mark offline if we crash
            c.will_set(will_topic, payload="offline", qos=0, retain=True)

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
        self.client.publish(topic, payload=payload, qos=0, retain=retain)

    def publish_json(self, topic: str, obj: Dict[str, Any], retain: bool = True) -> None:
        self.publish(topic, json.dumps(obj, ensure_ascii=False), retain=retain)

    def delete_retained(self, topic: str) -> None:
        # Deleting retained topic: publish empty payload retained
        if not self.client:
            return
        self.client.publish(topic, payload="", qos=0, retain=True)


# ===== Discovery publish =====
def publish_discovery_for_devices(
    mpub: MqttPub,
    cfg: Dict[str, Any],
    home_id: int,
    devices: List[Dict[str, Any]],
) -> None:
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

    for d in devices:
        did = d["id"]
        name = d["name"]
        obj_id, safe = discovery_object_ids(name)

        # 1) device_tracker for presence
        track_topic = f"{dp}/device_tracker/{ha_device_id}/home_{home_id}_device_{did}/config"
        track_payload: Dict[str, Any] = {
            "name": f"Tado Presence {name}",
            "unique_id": f"{ha_device_id}_presence_{home_id}_{did}",
            "state_topic": f"{tp}/presence/home_{home_id}/device_{did}/state",
            "payload_home": "home",
            "payload_not_home": "not_home",
            "source_type": "gps",
            "device": device_block,
        }
        mpub.publish_json(track_topic, track_payload, retain=True)

        # 2) raw JSON sensor per device (optional)
        if cfg.get("enable_raw_sensors", True):
            raw_topic = f"{dp}/sensor/{ha_device_id}/home_{home_id}_device_{did}_raw/config"
            raw_payload: Dict[str, Any] = {
                "name": f"Tado Presence {name} (raw)",
                "unique_id": f"{ha_device_id}_presence_raw_{home_id}_{did}",
                "state_topic": f"{tp}/presence/home_{home_id}/device_{did}/state",
                "json_attributes_topic": f"{tp}/presence/home_{home_id}/device_{did}/json",
                "icon": "mdi:code-json",
                "entity_category": "diagnostic",
                "device": device_block,
            }
            mpub.publish_json(raw_topic, raw_payload, retain=True)

    # 3) legacy aggregate raw sensor per home (optional)
    if cfg.get("enable_home_raw_sensor", True):
        agg_topic = f"{dp}/sensor/{ha_device_id}/home_{home_id}_raw/config"
        agg_payload: Dict[str, Any] = {
            "name": f"Tado Home {home_id} (raw)",
            "unique_id": f"{ha_device_id}_home_raw_{home_id}",
            "state_topic": f"{tp}/presence/home_{home_id}/json",
            "json_attributes_topic": f"{tp}/presence/home_{home_id}/json",
            "icon": "mdi:home",
            "entity_category": "diagnostic",
            "device": device_block,
        }
        mpub.publish_json(agg_topic, agg_payload, retain=True)


def load_discovery_state() -> Dict[str, Any]:
    st = read_json(os.path.join(DATA_DIR, "tado_assistant_discovery_state.json"))
    if not st:
        return {"homes": {}}
    if "homes" not in st:
        st["homes"] = {}
    return st


def save_discovery_state(st: Dict[str, Any]) -> None:
    write_json_atomic(os.path.join(DATA_DIR, "tado_assistant_discovery_state.json"), st)


def discovery_cleanup_removed_devices(
    mpub: MqttPub,
    cfg: Dict[str, Any],
    home_id: int,
    removed_device_ids: Set[int],
) -> None:
    if not mpub.client:
        return
    if not removed_device_ids:
        return

    dp = cfg["discovery_prefix"]
    ha_device_id = cfg["ha_device_id"]

    for did in removed_device_ids:
        # remove old retained discovery topics
        mpub.delete_retained(f"{dp}/device_tracker/{ha_device_id}/home_{home_id}_device_{did}/config")
        mpub.delete_retained(f"{dp}/sensor/{ha_device_id}/home_{home_id}_device_{did}_raw/config")


# ===== Auto-Assist MQTT switch =====
def load_assist_state() -> Dict[str, Any]:
    st = read_json(ASSIST_STATE_PATH) or {}
    if "enabled" not in st:
        st["enabled"] = False  # default OFF
    if "last_run" not in st:
        st["last_run"] = None
    if "last_action" not in st:
        st["last_action"] = None
    if "last_error" not in st:
        st["last_error"] = None
    if "discovery_reset_done" not in st:
        st["discovery_reset_done"] = False
    return st


def save_assist_state(st: Dict[str, Any]) -> None:
    write_json_atomic(ASSIST_STATE_PATH, st)


def publish_auto_assist_discovery(mpub: MqttPub, cfg: Dict[str, Any], st: Dict[str, Any]) -> None:
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

    # IMPORTANT: Keep topic + unique_id stable to avoid duplicates.
    config_topic = f"{dp}/switch/{ha_device_id}/auto_assist/config"

    # One-time reset to remove stale config that made entity 'unavailable'
    if not st.get("discovery_reset_done"):
        # delete current + some old variants once
        variants = [
            config_topic,
            f"{dp}/switch/{ha_device_id}/auto-assist/config",
            f"{dp}/switch/{ha_device_id}/{ha_device_id}_auto_assist/config",
            f"{dp}/switch/{ha_device_id}/{ha_device_id}_auto_assist_switch/config",
            f"{dp}/switch/{ha_device_id}/tado_assistant_auto_assist/config",
            f"{dp}/switch/{ha_device_id}/auto_assist_switch/config",
        ]
        for t in variants:
            mpub.delete_retained(t)
        st["discovery_reset_done"] = True
        save_assist_state(st)
        # small pause so HA processes delete
        time.sleep(0.5)

    payload: Dict[str, Any] = {
        "name": "Tado Assistant Auto-Assist",
        "unique_id": "tado_assistant_auto_assist",
        "object_id": "tado_assistant_auto_assist",
        "state_topic": f"{tp}/auto_assist/state",
        "command_topic": f"{tp}/auto_assist/set",
        "payload_on": "ON",
        "payload_off": "OFF",
        "state_on": "ON",
        "state_off": "OFF",
        "json_attributes_topic": f"{tp}/auto_assist/attrs",
        "icon": "mdi:auto-fix",
        "entity_category": "config",
        # NOTE: NO availability_topic here -> prevents "Diese Entität ist nicht verfügbar"
        "device": device_block,
    }

    mpub.publish_json(config_topic, payload, retain=True)
    log(f"auto_assist discovery published: {config_topic}")


def publish_auto_assist_state(mpub: MqttPub, cfg: Dict[str, Any], st: Dict[str, Any]) -> None:
    tp = cfg["topic_prefix"]
    enabled = bool(st.get("enabled", False))

    mpub.publish(f"{tp}/auto_assist/state", "ON" if enabled else "OFF", retain=True)

    attrs = {
        "enabled": enabled,
        "last_run": st.get("last_run"),
        "last_action": st.get("last_action"),
        "last_error": st.get("last_error"),
        "_ts": now_iso(),
    }
    mpub.publish_json(f"{tp}/auto_assist/attrs", attrs, retain=True)


def main() -> None:
    cfg = load_config()
    poll = cfg["poll_seconds"]
    tp = cfg["topic_prefix"]

    log(f"starting. poll_seconds={poll}")

    mpub = MqttPub(cfg["mqtt"])
    mpub.start(will_topic=f"{tp}/_status")

    # availability online (retained)
    if mpub.client:
        mpub.publish(f"{tp}/_status", "online", retain=True)
        log(f"status published: {tp}/_status = online")

    # Auto-Assist: publish discovery + initial state and subscribe commands
    assist_state = load_assist_state()
    if mpub.client:
        publish_auto_assist_discovery(mpub, cfg, assist_state)
        publish_auto_assist_state(mpub, cfg, assist_state)

        cmd_topic = f"{tp}/auto_assist/set"

        def on_message(client, userdata, msg):
            try:
                t = msg.topic
                p = (msg.payload or b"").decode("utf-8", errors="ignore").strip()
                if t != cmd_topic:
                    return
                log(f"auto_assist command received: {p}")
                if p.upper() in ("ON", "1", "TRUE"):
                    assist_state["enabled"] = True
                    assist_state["last_action"] = "enabled"
                    assist_state["last_error"] = None
                elif p.upper() in ("OFF", "0", "FALSE"):
                    assist_state["enabled"] = False
                    assist_state["last_action"] = "disabled"
                    assist_state["last_error"] = None
                else:
                    assist_state["last_error"] = f"unknown command payload: {p}"
                assist_state["last_run"] = now_iso()
                save_assist_state(assist_state)
                publish_auto_assist_state(mpub, cfg, assist_state)
            except Exception as e:
                log(f"ERROR in on_message: {e}")

        mpub.client.on_message = on_message
        mpub.client.subscribe(cmd_topic)
        log(f"auto_assist subscribed: {cmd_topic}")

    discovery_state = load_discovery_state()
    loop = 0

    # First loop runs immediately so HA gets state quickly
    while True:
        loop += 1
        try:
            # Always republish auto-assist state periodically (helps after HA reload)
            if mpub.client:
                publish_auto_assist_state(mpub, cfg, assist_state)

            if not tokens_exist():
                log(f"waiting for tokens: {TOKENS_PATH} (login first)")
                time.sleep(5)
                continue

            tokens = ensure_valid_tokens()

            # Homes
            home_ids = get_home_ids(tokens)
            if loop == 1:
                log(f"home ids resolved: {home_ids}")

            for home_id in home_ids:
                # Mobile devices presence
                try:
                    raw_devices = get_mobile_devices(tokens, home_id)
                except requests.HTTPError as he:
                    if getattr(he, "response", None) is not None and he.response.status_code == 429:
                        # backoff: use poll as minimum
                        backoff = max(poll, 120)
                        log(f"ERROR: Tado rate limit (429) on /homes/{home_id}/mobileDevices -> backoff {backoff}s")
                        time.sleep(backoff)
                        continue
                    raise

                devices = [normalize_presence(d) for d in raw_devices if d.get("id")]

                # Publish aggregate JSON (home-level)
                if mpub.client:
                    # Aggregate JSON for legacy "home_raw" sensor
                    agg_topic = f"{tp}/presence/home_{home_id}/json"
                    mpub.publish_json(
                        agg_topic,
                        {"home_id": home_id, "devices": devices, "_ts": now_iso()},
                        retain=True,
                    )

                    # Per device publish
                    for d in devices:
                        did = d.get("id")
                        if not did:
                            continue
                        base = f"{tp}/presence/home_{home_id}/device_{did}"
                        mpub.publish_json(base + "/json", d, retain=True)
                        mpub.publish(base + "/state", d["state"], retain=True)

                    # Discovery + cleanup
                    publish_discovery_for_devices(mpub, cfg, home_id, devices)

                    current_ids = {int(d["id"]) for d in devices if d.get("id")}
                    prev_ids = set(discovery_state["homes"].get(str(home_id), {}).get("device_ids", []))
                    removed = prev_ids - current_ids

                    if removed:
                        discovery_cleanup_removed_devices(mpub, cfg, home_id, removed)

                    discovery_state["homes"].setdefault(str(home_id), {})["device_ids"] = sorted(list(current_ids))
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
