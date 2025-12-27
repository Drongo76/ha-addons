#!/usr/bin/env python3
import os
import json
import time
import datetime as dt
import traceback
from typing import Any, Dict, Optional, List

import requests
import paho.mqtt.client as mqtt


DATA_DIR = "/data"

# WICHTIG: eigener Dateiname -> kein Konflikt mit HA offizieller Tado Integration
TOKEN_FILE = os.path.join(DATA_DIR, "tado_assistant_tokens.json")
LAST_TOKEN_RESPONSE_FILE = os.path.join(DATA_DIR, "tado_assistant_last_token_response.json")

# Tado REST API
TADO_API_BASE = "https://my.tado.com/api/v2"

# Tado OAuth Token Endpoint (Fallback, falls nicht im Token-JSON gespeichert)
DEFAULT_TOKEN_URL = "https://auth.tado.com/oauth/token"

# Client ID (Fallback, falls nicht im Token-JSON gespeichert)
# Muss zu deiner app.py passen. Wenn app.py im Token-File client_id speichert, wird das automatisch verwendet.
DEFAULT_CLIENT_ID = os.environ.get("TADO_CLIENT_ID", "tado-web-app")

REQUEST_TIMEOUT = 20
REFRESH_SAFETY_SECONDS = 90  # refresh, wenn Token in < 90s abläuft


# ------------------------- utils -------------------------

def log(msg: str) -> None:
    print(f"[worker] {msg}", flush=True)


def _read_json(path: str) -> Optional[Dict[str, Any]]:
    try:
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            txt = f.read().strip()
            if not txt:
                return None
            return json.loads(txt)
    except Exception as e:
        log(f"ERROR reading json {path}: {e}")
        return None


def _write_json(path: str, data: Dict[str, Any]) -> None:
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _now_utc_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _epoch() -> int:
    return int(time.time())


def _bool(v: Any) -> bool:
    return bool(v) is True


# ------------------------- options -------------------------

def load_options() -> Dict[str, Any]:
    """
    Lädt HA Add-on Optionen aus /data/options.json
    Unterstützt alte/verschiedene Key-Layouts.
    """
    opt = _read_json(os.path.join(DATA_DIR, "options.json")) or {}

    # Defaults
    poll_seconds = int(opt.get("poll_seconds", 30))

    # MQTT keys (dein aktuelles UI zeigt mqtt_enabled usw.)
    mqtt_enabled = opt.get("mqtt_enabled", None)
    # Fallback: manche Worker nutzen opt["mqtt"]["enabled"]
    if mqtt_enabled is None and isinstance(opt.get("mqtt"), dict):
        mqtt_enabled = opt["mqtt"].get("enabled", False)

    mqtt_host = opt.get("mqtt_host") or (opt.get("mqtt", {}) or {}).get("host")
    mqtt_port = int(opt.get("mqtt_port") or (opt.get("mqtt", {}) or {}).get("port") or 1883)
    mqtt_username = opt.get("mqtt_username") or (opt.get("mqtt", {}) or {}).get("username") or ""
    mqtt_password = opt.get("mqtt_password") or (opt.get("mqtt", {}) or {}).get("password") or ""

    mqtt_topic_prefix = opt.get("mqtt_topic_prefix", "tado_assistant").strip() or "tado_assistant"
    mqtt_discovery_prefix = opt.get("mqtt_discovery_prefix", "homeassistant").strip() or "homeassistant"

    out = {
        "poll_seconds": max(5, poll_seconds),
        "mqtt_enabled": _bool(mqtt_enabled),
        "mqtt_host": mqtt_host,
        "mqtt_port": mqtt_port,
        "mqtt_username": mqtt_username,
        "mqtt_password": mqtt_password,
        "mqtt_topic_prefix": mqtt_topic_prefix,
        "mqtt_discovery_prefix": mqtt_discovery_prefix,
    }

    log(f"options raw keys={list(opt.keys())}")
    return out


# ------------------------- tokens / refresh -------------------------

def load_tokens() -> Optional[Dict[str, Any]]:
    tok = _read_json(TOKEN_FILE)
    return tok


def token_meta(tok: Dict[str, Any]) -> Dict[str, Any]:
    """
    Unterstützt verschiedene Speichermodelle.
    """
    meta = {}
    if isinstance(tok.get("_meta"), dict):
        meta.update(tok["_meta"])
    # auch Top-Level akzeptieren
    for k in ("client_id", "client_secret", "token_url", "token_endpoint"):
        if k in tok and tok[k]:
            meta[k] = tok[k]
    return meta


def token_expires_at(tok: Dict[str, Any]) -> Optional[int]:
    """
    Erlaubt:
    - expires_at (epoch int)
    - expires_in + saved_at
    """
    if isinstance(tok.get("expires_at"), (int, float)):
        return int(tok["expires_at"])
    if isinstance(tok.get("expires_in"), (int, float)) and isinstance(tok.get("saved_at"), (int, float)):
        return int(tok["saved_at"] + tok["expires_in"])
    return None


def should_refresh(tok: Dict[str, Any]) -> bool:
    exp = token_expires_at(tok)
    if exp is None:
        # wenn nichts bekannt ist, refresh nicht erzwingen
        return False
    return (_epoch() + REFRESH_SAFETY_SECONDS) >= exp


def refresh_tokens(tok: Dict[str, Any]) -> Dict[str, Any]:
    refresh_token = tok.get("refresh_token")
    if not refresh_token:
        raise RuntimeError("No refresh_token in token file")

    meta = token_meta(tok)
    token_url = meta.get("token_url") or meta.get("token_endpoint") or DEFAULT_TOKEN_URL
    client_id = meta.get("client_id") or DEFAULT_CLIENT_ID
    client_secret = meta.get("client_secret")

    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
    }
    if client_secret:
        data["client_secret"] = client_secret

    log(f"Refreshing token via {token_url} (client_id={client_id})")
    r = requests.post(token_url, data=data, timeout=REQUEST_TIMEOUT)
    body = None
    try:
        body = r.json()
    except Exception:
        body = {"_raw": r.text}

    _write_json(LAST_TOKEN_RESPONSE_FILE, body)

    if r.status_code >= 400:
        raise RuntimeError(f"Token refresh failed {r.status_code}: {body}")

    # Normalisieren
    new_tok = dict(tok)
    new_tok.update(body)

    # expires_at setzen
    if "expires_in" in body:
        new_tok["saved_at"] = _epoch()
        new_tok["expires_at"] = _epoch() + int(body["expires_in"])

    _write_json(TOKEN_FILE, new_tok)
    log("Token refreshed + saved")
    return new_tok


# ------------------------- tado api -------------------------

def api_get(tok: Dict[str, Any], path: str) -> Any:
    access_token = tok.get("access_token")
    if not access_token:
        raise RuntimeError("No access_token")
    url = f"{TADO_API_BASE}{path}"
    headers = {"Authorization": f"Bearer {access_token}"}
    r = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
    if r.status_code >= 400:
        raise RuntimeError(f"GET {path} failed {r.status_code}: {r.text[:300]}")
    return r.json()


def fetch_presence(tok: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Liefert:
    [
      { home_id: int, devices: [ {id,name,state,at_home,_ts, ...raw...}, ... ] }
    ]
    """
    me = api_get(tok, "/me")
    homes = me.get("homes", []) if isinstance(me, dict) else []

    out = []
    for h in homes:
        home_id = h.get("id")
        if not home_id:
            continue

        # mobileDevices liefert Geofencing / atHome-Infos
        devices = api_get(tok, f"/homes/{home_id}/mobileDevices")
        dev_list = []
        if isinstance(devices, list):
            for d in devices:
                dev_id = d.get("id")
                name = d.get("name") or d.get("deviceName") or str(dev_id)

                # atHome sitzt je nach API Version an verschiedenen Stellen
                at_home = None
                if isinstance(d.get("location"), dict) and "atHome" in d["location"]:
                    at_home = d["location"].get("atHome")
                elif "atHome" in d:
                    at_home = d.get("atHome")

                if at_home is True:
                    state = "home"
                elif at_home is False:
                    state = "not_home"
                else:
                    state = "unknown"

                item = dict(d)
                item.update({
                    "id": dev_id,
                    "name": name,
                    "state": state,
                    "at_home": True if state == "home" else (False if state == "not_home" else None),
                    "_ts": _now_utc_iso(),
                })
                dev_list.append(item)

        out.append({"home_id": int(home_id), "devices": dev_list, "_ts": _now_utc_iso()})
    return out


# ------------------------- mqtt discovery + publish -------------------------

def mqtt_connect(opt: Dict[str, Any]) -> mqtt.Client:
    host = opt.get("mqtt_host")
    port = opt.get("mqtt_port", 1883)
    user = opt.get("mqtt_username") or ""
    pw = opt.get("mqtt_password") or ""

    if not host:
        raise RuntimeError("mqtt_host is empty")

    client = mqtt.Client(client_id="", clean_session=True)
    if user:
        client.username_pw_set(user, pw)

    def on_connect(c, userdata, flags, rc):
        log(f"MQTT connected rc={rc}")

    def on_disconnect(c, userdata, rc):
        log(f"MQTT disconnected rc={rc}")

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect

    log(f"MQTT connecting to {host}:{port} (user={'yes' if user else 'no'})")
    client.connect(host, port, keepalive=60)
    client.loop_start()
    return client


def publish_discovery(client: mqtt.Client, opt: Dict[str, Any], home_id: int, dev: Dict[str, Any]) -> None:
    """
    Erstellt 2 Entities:
    - device_tracker (Presence): home/not_home ✅
    - sensor (raw): home/not_home/unknown + json attributes
    """
    dp = opt["mqtt_discovery_prefix"]
    tp = opt["mqtt_topic_prefix"]

    dev_id = dev["id"]
    safe_name = str(dev.get("name") or dev_id).strip()

    device_block = {
        "identifiers": [f"tado_assistant_{home_id}"],
        "name": "Tado Assistant",
        "manufacturer": "tado°",
        "model": "Tado Assistant (Ingress)",
    }

    state_topic_tracker = f"{tp}/presence/home_{home_id}/device_{dev_id}/tracker_state"
    state_topic_raw = f"{tp}/presence/home_{home_id}/device_{dev_id}/state_raw"
    json_attr_topic = f"{tp}/presence/home_{home_id}/device_{dev_id}/json"

    # device_tracker
    obj_id_trk = f"tado_presence_{home_id}_{dev_id}"
    disc_topic_trk = f"{dp}/device_tracker/{obj_id_trk}/config"
    payload_trk = {
        "name": f"Tado Presence {safe_name}",
        "unique_id": f"tado_assistant_tracker_{home_id}_{dev_id}",
        "device": device_block,
        "state_topic": state_topic_tracker,
        "payload_home": "home",
        "payload_not_home": "not_home",
        "json_attributes_topic": json_attr_topic,
        "source_type": "gps",
    }

    # raw sensor
    obj_id_raw = f"tado_presence_{home_id}_{dev_id}_raw"
    disc_topic_raw = f"{dp}/sensor/{obj_id_raw}/config"
    payload_raw = {
        "name": f"Tado Presence {safe_name} (raw)",
        "unique_id": f"tado_assistant_presence_raw_{home_id}_{dev_id}",
        "device": device_block,
        "state_topic": state_topic_raw,
        "json_attributes_topic": json_attr_topic,
    }

    client.publish(disc_topic_trk, json.dumps(payload_trk), qos=0, retain=True)
    client.publish(disc_topic_raw, json.dumps(payload_raw), qos=0, retain=True)


def publish_presence(client: mqtt.Client, opt: Dict[str, Any], presence: List[Dict[str, Any]]) -> None:
    tp = opt["mqtt_topic_prefix"]
    now = _now_utc_iso()

    for h in presence:
        home_id = h["home_id"]
        devices = h.get("devices", [])

        # optional: home json
        client.publish(
            f"{tp}/presence/home_{home_id}",
            json.dumps({"home_id": home_id, "devices": devices, "_ts": now}),
            qos=0,
            retain=True,
        )

        for d in devices:
            dev_id = d["id"]
            raw_state = (d.get("state") or "unknown").strip()

            # raw sensor state (auch unknown)
            client.publish(
                f"{tp}/presence/home_{home_id}/device_{dev_id}/state_raw",
                raw_state,
                qos=0,
                retain=True,
            )

            # device_tracker state: nur home/not_home publishen
            # unknown -> nicht publishen => HA behält letzten retained Zustand
            if raw_state in ("home", "not_home"):
                client.publish(
                    f"{tp}/presence/home_{home_id}/device_{dev_id}/tracker_state",
                    raw_state,
                    qos=0,
                    retain=True,
                )

            # json attributes
            client.publish(
                f"{tp}/presence/home_{home_id}/device_{dev_id}/json",
                json.dumps(d),
                qos=0,
                retain=True,
            )


# ------------------------- main loop -------------------------

def main() -> None:
    log("Worker started")

    mqtt_client: Optional[mqtt.Client] = None
    discovered = set()

    while True:
        try:
            opt = load_options()

            # MQTT connect/disconnect je nach Option
            if opt["mqtt_enabled"]:
                if mqtt_client is None:
                    mqtt_client = mqtt_connect(opt)
            else:
                if mqtt_client is not None:
                    try:
                        mqtt_client.loop_stop()
                        mqtt_client.disconnect()
                    except Exception:
                        pass
                    mqtt_client = None
                log("MQTT disabled (options.json mqtt_enabled=false)")

            tok = load_tokens()
            if not tok:
                log(f"No tokens found at {TOKEN_FILE}. Please login via Ingress UI first.")
                time.sleep(opt["poll_seconds"])
                continue

            # refresh wenn nötig
            if should_refresh(tok):
                try:
                    tok = refresh_tokens(tok)
                except Exception as e:
                    log(f"ERROR refreshing token: {e}")
                    time.sleep(opt["poll_seconds"])
                    continue

            # presence holen
            presence = fetch_presence(tok)
            total = sum(len(h.get("devices", [])) for h in presence)
            log(f"Fetched presence: homes={len(presence)} devices={total}")

            # publish
            if mqtt_client is not None:
                for h in presence:
                    home_id = h["home_id"]
                    for d in h.get("devices", []):
                        key = (home_id, d.get("id"))
                        # retained discovery darf man immer senden,
                        # aber wir reduzieren Spam und schicken discovery nur einmal pro Device
                        if key not in discovered:
                            publish_discovery(mqtt_client, opt, home_id, d)
                            discovered.add(key)

                publish_presence(mqtt_client, opt, presence)

        except Exception as e:
            log(f"FATAL loop error: {e}")
            traceback.print_exc()

        time.sleep(load_options().get("poll_seconds", 30))


if __name__ == "__main__":
    main()
