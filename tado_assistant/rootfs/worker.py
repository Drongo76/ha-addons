#!/usr/bin/env python3
import json
import os
import re
import time
import requests
import paho.mqtt.client as mqtt

APP_DIR = "/data/tado_assistant"
AUTH_FILE = os.path.join(APP_DIR, "auth.json")
OPTIONS_FILE = "/data/options.json"

os.makedirs(APP_DIR, exist_ok=True)

DEVICE_AUTH_URL = "https://login.tado.com/oauth2/device"
TOKEN_URL = "https://login.tado.com/oauth2/token"
API_BASE = "https://my.tado.com/api/v2"

# Öffentliches Client (Device Code Flow)
TADO_CLIENT_ID = "public-api-preview"

def load_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default if default is not None else {}

def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

def slugify(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9]+", "_", s).strip("_")
    return s or "x"

def sanitize_name(name: str) -> str:
    name = (name or "").strip().lower()
    name = name.replace(" ", "_").replace("-", "_").replace("/", "_")
    name = re.sub(r"[^a-z0-9_]+", "", name)
    name = re.sub(r"_+", "_", name).strip("_")
    return name or "device"

def api_get(access_token: str, path: str):
    r = requests.get(
        f"{API_BASE}{path}",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=20,
    )
    r.raise_for_status()
    return r.json()

def refresh_tokens(refresh_token: str):
    r = requests.post(
        TOKEN_URL,
        data={
            "client_id": TADO_CLIENT_ID,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
        timeout=20,
    )
    r.raise_for_status()
    return r.json()

def ensure_tokens(auth: dict) -> dict:
    # Wenn Token fehlt: Worker kann keine States liefern.
    if not auth.get("access_token") or not auth.get("refresh_token"):
        return auth

    # Refresh wenn alt
    now = int(time.time())
    expires_at = int(auth.get("expires_at", 0))
    if expires_at and now < (expires_at - 30):
        return auth

    try:
        tokens = refresh_tokens(auth["refresh_token"])
        auth["access_token"] = tokens["access_token"]
        auth["refresh_token"] = tokens.get("refresh_token", auth["refresh_token"])
        auth["expires_at"] = int(time.time()) + int(tokens.get("expires_in", 3600))
        auth["message"] = "Token refreshed"
        save_json(AUTH_FILE, auth)
        return auth
    except Exception as e:
        auth["message"] = f"Token refresh failed: {e}"
        save_json(AUTH_FILE, auth)
        return auth

def publish_discovery(client, disc_prefix, base_topic, home_id, devices):
    device_block = {
        "identifiers": [f"tado_assistant_{home_id}"],
        "manufacturer": "tado°",
        "name": "Tado Assistant",
        "model": "Add-on",
    }

    for d in devices:
        did = d.get("id")
        name = d.get("name") or f"Device {did}"
        safe = sanitize_name(name)
        object_id = f"tado_assistant_{safe}_{did}_home"
        slug = slugify(object_id)

        client.publish(
            f"{disc_prefix}/binary_sensor/tado_assistant_presence/{slug}/config",
            json.dumps(
                {
                    "name": f"Tado {name} Home",
                    "unique_id": f"tado_assistant_presence_{home_id}_mobile_{did}",
                    "state_topic": f"{base_topic}/{object_id}/state",
                    "payload_on": "home",
                    "payload_off": "away",
                    "device_class": "presence",
                    "device": device_block,
                }
            ),
            retain=True,
        )

    client.publish(
        f"{disc_prefix}/sensor/tado_assistant_presence/anyone_home_count/config",
        json.dumps(
            {
                "name": "Tado Anyone Home (Count)",
                "unique_id": f"tado_assistant_anyone_home_count_{home_id}",
                "state_topic": f"{base_topic}/tado_assistant_anyone_home_count/state",
                "unit_of_measurement": "persons",
                "device": device_block,
            }
        ),
        retain=True,
    )

def publish_states(client, base_topic, devices):
    home_count = 0

    for d in devices:
        did = d.get("id")
        name = d.get("name") or f"Device {did}"
        safe = sanitize_name(name)
        object_id = f"tado_assistant_{safe}_{did}_home"

        at_home = bool((d.get("location") or {}).get("atHome", False))
        state = "home" if at_home else "away"
        if at_home:
            home_count += 1

        client.publish(f"{base_topic}/{object_id}/state", state, retain=True)

    client.publish(
        f"{base_topic}/tado_assistant_anyone_home_count/state",
        str(home_count),
        retain=True,
    )

def mqtt_connect(opts):
    host = (opts.get("mqtt_host") or "").strip()
    port = int(opts.get("mqtt_port", 1883))
    user = (opts.get("mqtt_username") or "").strip()
    pwd = (opts.get("mqtt_password") or "")

    if not host:
        raise RuntimeError("mqtt_host ist leer. Trage IP/Host in Add-on Konfiguration ein.")

    client = mqtt.Client()
    if user:
        client.username_pw_set(user, pwd)

    client.connect(host, port, keepalive=60)
    client.loop_start()
    return client

def fetch_mobile_devices(access_token: str, home_id: int):
    return api_get(access_token, f"/homes/{home_id}/mobileDevices")

def main():
    opts = load_json(OPTIONS_FILE, {})
    poll_seconds = int(opts.get("poll_seconds", 30))
    mqtt_enabled = bool(opts.get("mqtt_enabled", False))
    disc_prefix = "homeassistant"
    base_topic = (opts.get("mqtt_base_topic") or "tado_assistant").strip()

    # Ohne MQTT: nichts zu tun (dann bleiben Entities „Unbekannt“)
    if not mqtt_enabled:
        while True:
            time.sleep(60)

    client = mqtt_connect(opts)

    discovery_done = False

    while True:
        auth = load_json(AUTH_FILE, {})
        auth = ensure_tokens(auth)

        access = auth.get("access_token")
        home_id = auth.get("home_id")

        if not access or not home_id:
            # Noch kein Token/HomeId → warten
            time.sleep(5)
            continue

        try:
            devices = fetch_mobile_devices(access, int(home_id))

            if not discovery_done:
                publish_discovery(client, disc_prefix, base_topic, int(home_id), devices)
                discovery_done = True

            publish_states(client, base_topic, devices)

        except Exception as e:
            auth["message"] = f"Worker error: {e}"
            save_json(AUTH_FILE, auth)

        time.sleep(poll_seconds)

if __name__ == "__main__":
    main()
