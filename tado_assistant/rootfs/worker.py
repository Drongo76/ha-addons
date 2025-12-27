#!/usr/bin/env python3
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
import paho.mqtt.client as mqtt

# -----------------------------
# Files (keine Konflikte mit HA original Tado)
# -----------------------------
DATA_DIR = Path("/data")
OPTIONS_FILE = DATA_DIR / "options.json"

TOKEN_FILE = DATA_DIR / "tado_assistant_tokens.json"
LAST_TOKEN_RESPONSE_FILE = DATA_DIR / "tado_assistant_last_token_response.json"

# -----------------------------
# Tado OAuth / API (laut offizieller Doku)
# -----------------------------
TADO_CLIENT_ID = "1bb50063-6b0c-4d11-bd99-387f4a91cc46"  # device-code flow client_id :contentReference[oaicite:1]{index=1}
TADO_TOKEN_URL = "https://login.tado.com/oauth2/token"   # refresh/token endpoint :contentReference[oaicite:2]{index=2}
TADO_API_BASE = "https://my.tado.com/api/v2"


# -----------------------------
# Helpers
# -----------------------------
def log(msg: str) -> None:
    print(f"[worker] {msg}", flush=True)


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except Exception as e:
        log(f"ERROR reading JSON {path}: {e}")
        return default


def _write_json_atomic(path: Path, data: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def slugify(name: str) -> str:
    s = name.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s or "device"


def load_options() -> Dict[str, Any]:
    opts = _read_json(OPTIONS_FILE, default={}) or {}

    # Defaults
    opts.setdefault("poll_seconds", 30)
    opts.setdefault("mqtt_enabled", False)
    opts.setdefault("mqtt_host", "")
    opts.setdefault("mqtt_port", 1883)
    opts.setdefault("mqtt_username", "")
    opts.setdefault("mqtt_password", "")
    opts.setdefault("mqtt_topic_prefix", "tado_assistant")
    opts.setdefault("mqtt_discovery_prefix", "homeassistant")
    return opts


def tokens_are_expiring(tokens: Dict[str, Any], skew_seconds: int = 60) -> bool:
    """
    token response from Tado has 'expires_in' (seconds).
    We add 'obtained_at' when we save it (ISO).
    """
    if not tokens:
        return True

    expires_in = tokens.get("expires_in")
    obtained_at = tokens.get("obtained_at")

    # If we don't know timing → be conservative.
    if not expires_in or not obtained_at:
        return True

    try:
        obt = datetime.fromisoformat(obtained_at.replace("Z", "+00:00"))
    except Exception:
        return True

    exp_ts = obt.timestamp() + int(expires_in)
    now_ts = time.time()
    return (exp_ts - now_ts) < skew_seconds


def refresh_tokens(tokens: Dict[str, Any]) -> Dict[str, Any]:
    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        raise RuntimeError("No refresh_token available (did you request scope=offline_access?)")

    params = {
        "client_id": TADO_CLIENT_ID,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }
    r = requests.post(TADO_TOKEN_URL, params=params, timeout=20)
    if r.status_code >= 400:
        raise RuntimeError(f"Refresh failed HTTP {r.status_code}: {r.text[:200]}")

    new_tokens = r.json()
    # refresh token rotation: new refresh_token replaces the old one :contentReference[oaicite:3]{index=3}
    if "refresh_token" not in new_tokens:
        new_tokens["refresh_token"] = refresh_token

    new_tokens["obtained_at"] = utc_iso()

    _write_json_atomic(TOKEN_FILE, new_tokens)
    _write_json_atomic(LAST_TOKEN_RESPONSE_FILE, new_tokens)
    return new_tokens


def ensure_tokens() -> Dict[str, Any]:
    tokens = _read_json(TOKEN_FILE, default={}) or {}
    if not tokens:
        raise RuntimeError(f"Token file missing/empty: {TOKEN_FILE}")

    # If no obtained_at exists, add it (best-effort) so refresh timing works.
    if "obtained_at" not in tokens and "expires_in" in tokens:
        tokens["obtained_at"] = utc_iso()
        _write_json_atomic(TOKEN_FILE, tokens)

    if tokens_are_expiring(tokens):
        log("Token expiring → refreshing…")
        tokens = refresh_tokens(tokens)
        log("Token refresh OK")

    return tokens


def tado_get(path: str, access_token: str) -> Any:
    url = f"{TADO_API_BASE}{path}"
    headers = {"Authorization": f"Bearer {access_token}"}
    r = requests.get(url, headers=headers, timeout=25)
    if r.status_code >= 400:
        raise RuntimeError(f"Tado GET {path} failed HTTP {r.status_code}: {r.text[:200]}")
    return r.json()


def fetch_homes_and_devices(access_token: str) -> List[Tuple[Dict[str, Any], List[Dict[str, Any]]]]:
    """
    Returns: [(home_obj, mobile_devices_list), ...]
    """
    me = tado_get("/me", access_token)  # :contentReference[oaicite:4]{index=4}
    homes = me.get("homes", []) if isinstance(me, dict) else []
    out: List[Tuple[Dict[str, Any], List[Dict[str, Any]]]] = []
    for h in homes:
        hid = h.get("id")
        if not hid:
            continue
        devices = tado_get(f"/homes/{hid}/mobileDevices", access_token)
        if not isinstance(devices, list):
            devices = []
        out.append((h, devices))
    return out


# -----------------------------
# MQTT
# -----------------------------
def mqtt_connect(opts: Dict[str, Any]) -> mqtt.Client:
    host = opts.get("mqtt_host", "")
    port = int(opts.get("mqtt_port", 1883))
    user = opts.get("mqtt_username", "") or None
    pwd = opts.get("mqtt_password", "") or None

    client = mqtt.Client()
    if user:
        client.username_pw_set(user, pwd)

    # Availability (LWT)
    topic_prefix = opts["mqtt_topic_prefix"].strip("/")
    availability_topic = f"{topic_prefix}/status"
    client.will_set(availability_topic, payload="offline", qos=0, retain=True)

    log(f"MQTT connecting to {host}:{port} (user={'yes' if user else 'no'})")
    client.connect(host, port, keepalive=60)
    client.loop_start()

    # Mark online
    client.publish(availability_topic, payload="online", qos=0, retain=True)
    return client


def publish_discovery_for_home_and_devices(
    client: mqtt.Client,
    opts: Dict[str, Any],
    home: Dict[str, Any],
    devices: List[Dict[str, Any]],
) -> None:
    disc = opts["mqtt_discovery_prefix"].strip("/")
    topic_prefix = opts["mqtt_topic_prefix"].strip("/")
    availability_topic = f"{topic_prefix}/status"

    home_id = home.get("id")
    home_name = home.get("name") or f"Home {home_id}"

    # One HA "device" bucket for all entities
    device_block = {
        "identifiers": [f"tado_assistant_home_{home_id}"],
        "name": "Tado Assistant",
        "manufacturer": "tado°",
        "model": f"Presence (home {home_id})",
    }

    # ---- (A) HOME RAW SENSOR (damit wieder 7 Entities bei dir)
    home_state_topic = f"{topic_prefix}/presence/home_{home_id}/json"
    home_obj_id = f"tado_assistant_tado_presence_home_{home_id}_raw"
    home_cfg_topic = f"{disc}/sensor/{home_obj_id}/config"
    home_cfg = {
        "name": f"Tado Presence Home {home_id} (raw)",
        "unique_id": f"tado_assistant_presence_home_{home_id}_raw",
        "state_topic": home_state_topic,
        "value_template": "{{ value_json._ts }}",
        "json_attributes_topic": home_state_topic,
        "availability_topic": availability_topic,
        "payload_available": "online",
        "payload_not_available": "offline",
        "device": device_block,
    }
    client.publish(home_cfg_topic, json.dumps(home_cfg), retain=True)

    # ---- (B) PER DEVICE: device_tracker + raw sensor
    for d in devices:
        did = d.get("id")
        name = d.get("name") or d.get("deviceName") or str(did)
        sname = slugify(str(name))

        state_topic = f"{topic_prefix}/presence/home_{home_id}/device_{did}/state"
        json_topic = f"{topic_prefix}/presence/home_{home_id}/device_{did}/json"

        # device_tracker (Anwesenheit als home/not_home)
        trk_obj_id = f"tado_assistant_tado_presence_{sname}"
        trk_cfg_topic = f"{disc}/device_tracker/{trk_obj_id}/config"
        trk_cfg = {
            "name": f"Tado Presence {name}",
            "unique_id": f"tado_assistant_presence_{home_id}_{did}",
            "state_topic": state_topic,
            "json_attributes_topic": json_topic,
            "payload_home": "home",
            "payload_not_home": "not_home",
            "availability_topic": availability_topic,
            "payload_available": "online",
            "payload_not_available": "offline",
            "device": device_block,
        }
        client.publish(trk_cfg_topic, json.dumps(trk_cfg), retain=True)

        # raw sensor (volle JSON als Attribute)
        raw_obj_id = f"tado_assistant_tado_presence_{sname}_raw"
        raw_cfg_topic = f"{disc}/sensor/{raw_obj_id}/config"
        raw_cfg = {
            "name": f"Tado Presence {name} (raw)",
            "unique_id": f"tado_assistant_presence_{home_id}_{did}_raw",
            "state_topic": json_topic,
            "value_template": "{{ value_json.state }}",
            "json_attributes_topic": json_topic,
            "availability_topic": availability_topic,
            "payload_available": "online",
            "payload_not_available": "offline",
            "device": device_block,
        }
        client.publish(raw_cfg_topic, json.dumps(raw_cfg), retain=True)


def publish_presence(
    client: mqtt.Client,
    opts: Dict[str, Any],
    home: Dict[str, Any],
    devices: List[Dict[str, Any]],
) -> None:
    topic_prefix = opts["mqtt_topic_prefix"].strip("/")
    home_id = home.get("id")
    home_name = home.get("name") or f"Home {home_id}"

    # build normalized payloads
    normalized_devices: List[Dict[str, Any]] = []
    for d in devices:
        did = d.get("id")
        name = d.get("name") or d.get("deviceName") or str(did)

        loc = d.get("location") if isinstance(d.get("location"), dict) else {}
        at_home = loc.get("atHome")
        # fallback: sometimes "atHome" might exist elsewhere
        if at_home is None:
            at_home = d.get("atHome")

        if at_home is True:
            state = "home"
        elif at_home is False:
            state = "not_home"
        else:
            state = "unknown"

        payload = {
            "id": did,
            "name": name,
            "state": state,
            "at_home": True if state == "home" else False if state == "not_home" else None,
            "_ts": utc_iso(),
            # raw Tado parts (so du siehst alles in den Attributen):
            "location": d.get("location"),
            "settings": d.get("settings"),
            "deviceMetadata": d.get("deviceMetadata"),
        }

        normalized_devices.append(payload)

        # publish per-device
        state_topic = f"{topic_prefix}/presence/home_{home_id}/device_{did}/state"
        json_topic = f"{topic_prefix}/presence/home_{home_id}/device_{did}/json"
        client.publish(state_topic, state, retain=True)
        client.publish(json_topic, json.dumps(payload, ensure_ascii=False), retain=True)

    # publish home summary raw JSON (7. Entity)
    home_topic = f"{topic_prefix}/presence/home_{home_id}/json"
    home_payload = {
        "home_id": home_id,
        "home_name": home_name,
        "devices": normalized_devices,
        "_ts": utc_iso(),
    }
    client.publish(home_topic, json.dumps(home_payload, ensure_ascii=False), retain=True)


# -----------------------------
# Main
# -----------------------------
def main() -> None:
    opts = load_options()

    mqtt_enabled = bool(opts.get("mqtt_enabled"))
    if not mqtt_enabled:
        log("MQTT disabled (options.json mqtt_enabled=false)")
        # Still keep running so UI/login can happen; just idle.
        while True:
            time.sleep(60)

    client = mqtt_connect(opts)

    last_discovery: float = 0.0
    discovery_interval = 6 * 60.0  # refresh discovery every 6 minutes (harmlos, retained)

    while True:
        try:
            opts = load_options()  # allow live changes

            tokens = ensure_tokens()
            access_token = tokens.get("access_token")
            if not access_token:
                raise RuntimeError("Token file has no access_token")

            homes_and_devices = fetch_homes_and_devices(access_token)

            now = time.time()
            if (now - last_discovery) > discovery_interval:
                for home, devs in homes_and_devices:
                    publish_discovery_for_home_and_devices(client, opts, home, devs)
                last_discovery = now
                log("MQTT discovery published/refreshed")

            for home, devs in homes_and_devices:
                publish_presence(client, opts, home, devs)

            time.sleep(int(opts.get("poll_seconds", 30)))

        except Exception as e:
            log(f"ERROR: {e}")
            time.sleep(10)


if __name__ == "__main__":
    main()
