import json
import os
import re
import time
import requests
import paho.mqtt.client as mqtt

AUTH_FILE = "/data/tado_assistant/auth.json"
OPTIONS_FILE = "/data/options.json"

TADO_CLIENT_ID = "1bb50063-6b0c-4d11-bd99-387f4a91cc46"
TOKEN_URL = "https://login.tado.com/oauth2/token"
MOBILE_DEVICES_URL = "https://my.tado.com/api/v2/homes/{home_id}/mobileDevices"

def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def slugify(s: str) -> str:
    s = s.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "_", s)
    return s.strip("_") or "device"

def refresh_access_token(refresh_token: str) -> str:
    r = requests.post(
        TOKEN_URL,
        params={
            "client_id": TADO_CLIENT_ID,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
        timeout=25,
    )
    r.raise_for_status()
    data = r.json()
    return data["access_token"]

def fetch_mobile_devices(access_token: str, home_id: int):
    r = requests.get(
        MOBILE_DEVICES_URL.format(home_id=home_id),
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=25,
    )
    r.raise_for_status()
    return r.json()

def mqtt_connect(opts):
    host = opts.get("mqtt_host", "")
    port = int(opts.get("mqtt_port", 1883))
    user = opts.get("mqtt_username", "")
    pwd = opts.get("mqtt_password", "")

    client = mqtt.Client()
    if user:
        client.username_pw_set(user, pwd)
    client.connect(host, port, keepalive=30)
    client.loop_start()
    return client

def publish_discovery(client, disc_prefix, base_topic, home_id, devices):
    # Gesamt-Sensor: Anzahl zuhause
    cfg_topic = f"{disc_prefix}/sensor/tado_assistant/home_count/config"
    state_topic = f"{base_topic}/home_count/state"

    payload = {
        "name": "Tado Anyone Home (Count)",
        "unique_id": f"tado_assistant_{home_id}_home_count",
        "state_topic": state_topic,
        "icon": "mdi:home-account",
        "unit_of_measurement": "persons",
        "device": {
            "identifiers": [f"tado_assistant_{home_id}"],
            "name": "Tado Assistant",
            "manufacturer": "tado° (via Add-on)",
            "model": "Tado Assistant",
        },
    }
    client.publish(cfg_topic, json.dumps(payload), retain=True)

    # Pro Mobile Device: binary_sensor presence
    for d in devices:
        did = d.get("id")
        name = d.get("name") or f"Device {did}"
        slug = slugify(f"{name}_{did}")

        cfg_topic = f"{disc_prefix}/binary_sensor/tado_assistant/{slug}/config"
        state_topic = f"{base_topic}/mobile_devices/{did}/state"

        payload = {
            "name": f"Tado {name} Home",
            "unique_id": f"tado_assistant_{home_id}_mobile_{did}",
            "state_topic": state_topic,
            "payload_on": "home",
            "payload_off": "away",
            "device_class": "presence",
            "device": {
                "identifiers": [f"tado_assistant_{home_id}"],
                "name": "Tado Assistant",
                "manufacturer": "tado° (via Add-on)",
                "model": "Tado Assistant",
            },
        }
        client.publish(cfg_topic, json.dumps(payload), retain=True)

def publish_states(client, base_topic, devices):
    home_count = 0
    for d in devices:
        did = d.get("id")
        at_home = bool(d.get("location", {}).get("atHome", False))
        state = "home" if at_home else "away"
        if at_home:
            home_count += 1
        client.publish(f"{base_topic}/mobile_devices/{did}/state", state, retain=True)

    client.publish(f"{base_topic}/home_count/state", str(home_count), retain=True)

def main():
    print("[worker] start")

    opts = load_json(OPTIONS_FILE, {})
    if not opts.get("mqtt_enabled", False):
        print("[worker] mqtt_enabled=false -> nichts zu tun")
        while True:
            time.sleep(3600)

    disc_prefix = opts.get("mqtt_discovery_prefix", "homeassistant")
    base_topic = opts.get("mqtt_base_topic", "tado_assistant")
    poll_seconds = int(opts.get("poll_seconds", 60))

    auth = load_json(AUTH_FILE, {})
    refresh_token = auth.get("refresh_token")
    home_id = auth.get("home_id")

    if not refresh_token or not home_id:
        print("[worker] kein auth.json (refresh_token/home_id fehlt) -> warte")
        while True:
            time.sleep(30)

    client = mqtt_connect(opts)
    print("[worker] mqtt connected")

    # Discovery einmal senden (retained)
    access = refresh_access_token(refresh_token)
    devices = fetch_mobile_devices(access, int(home_id))
    publish_discovery(client, disc_prefix, base_topic, int(home_id), devices)
    print(f"[worker] discovery published: {len(devices)} devices")

    # Loop: States aktualisieren
    while True:
        try:
            access = refresh_access_token(refresh_token)
            devices = fetch_mobile_devices(access, int(home_id))
            publish_states(client, base_topic, devices)
            print(f"[worker] states published: {len(devices)} devices")
        except Exception as e:
            print(f"[worker] error: {e}")
        time.sleep(poll_seconds)

if __name__ == "__main__":
    main()
