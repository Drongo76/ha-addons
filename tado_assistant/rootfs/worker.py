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


def write_json(path: str, data: dict):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def slugify(s: str) -> str:
    s = s.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "_", s)
    return s.strip("_") or "device"


def refresh_tokens(refresh_token: str):
    # WICHTIG: FORM body, NICHT params (sonst landet Token in URL) 4
    r = requests.post(
        TOKEN_URL,
        data={
            "client_id": TADO_CLIENT_ID,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )

    if r.status_code != 200:
        # invalid_grant => Token ist tot, dann nicht crashen, sondern warten
        try:
            body = r.json()
        except Exception:
            body = {"raw": r.text}

        if isinstance(body, dict) and body.get("error") == "invalid_grant":
            raise RuntimeError(f"AUTH_INVALID: {body}")

        raise RuntimeError(f"TOKEN_REFRESH_FAILED: {r.status_code} {r.text}")

    data = r.json()
    access = data.get("access_token")
    new_refresh = data.get("refresh_token")  # kann rotieren!
    if not access:
        raise RuntimeError("TOKEN_REFRESH_FAILED: access_token missing")
    return access, (new_refresh or refresh_token)


def fetch_mobile_devices(access_token: str, home_id: int):
    r = requests.get(
        MOBILE_DEVICES_URL.format(home_id=home_id),
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def mqtt_connect(opts):
    host = (opts.get("mqtt_host") or "").strip()
    port = int(opts.get("mqtt_port", 1883))
    user = (opts.get("mqtt_username") or "").strip()
    pwd = opts.get("mqtt_password") or ""

    if not host:
        raise RuntimeError("mqtt_host ist leer. Bitte im Add-on -> Konfiguration setzen.")

    client = mqtt.Client()
    if user:
        client.username_pw_set(user, pwd)

    client.connect(host, port, keepalive=30)
    client.loop_start()
    return client


def publish_discovery(client, disc_prefix, base_topic, home_id, devices):
    device_block = {
        "identifiers": [f"tado_assistant_{home_id}"],
        "name": "Tado Assistant",
        "manufacturer": "tado° (via Add-on)",
        "model": "Tado Assistant",
    }

    # Count Sensor
    client.publish(
        f"{disc_prefix}/sensor/tado_assistant/home_count/config",
        json.dumps(
            {
                "name": "Tado Anyone Home (Count)",
                "unique_id": f"tado_assistant_{home_id}_home_count",
                "state_topic": f"{base_topic}/home_count/state",
                "icon": "mdi:home-account",
                "unit_of_measurement": "persons",
                "device": device_block,
            }
        ),
        retain=True,
    )

    # Presence Binary Sensors (eigener Discovery-Pfad => keine Vermischung)
    for d in devices:
        did = d.get("id")
        name = d.get("name") or f"Device {did}"
        slug = slugify(f"{name}_{did}")

        client.publish(
            f"{disc_prefix}/binary_sensor/tado_assistant_presence/{slug}/config",
            json.dumps(
                {
                    "name": f"Tado {name} Home",
                    "unique_id": f"tado_assistant_presence_{home_id}_mobile_{did}",
                    "state_topic": f"{base_topic}/mobile_devices/{did}/state",
                    "payload_on": "home",
                    "payload_off": "away",
                    "device_class": "presence",
                    "device": device_block,
                }
            ),
            retain=True,
        )


def publish_states(client, base_topic, devices):
    home_count = 0

    for d in devices:
        did = d.get("id")
        at_home = bool((d.get("location") or {}).get("atHome", False))
        state = "home" if at_home else "away"
        if at_home:
            home_count += 1

        client.publish(f"{base_topic}/mobile_devices/{did}/state", state, retain=True)

    client.publish(f"{base_topic}/home_count/state", str(home_count), retain=True)


def main():
    print("[worker] start")

    opts = load_json(OPTIONS_FILE, {})
    if not bool(opts.get("mqtt_enabled", False)):
        print("[worker] mqtt_enabled=false -> stop")
        while True:
            time.sleep(3600)

    disc_prefix = opts.get("mqtt_discovery_prefix", "homeassistant")
    base_topic = opts.get("mqtt_base_topic", "tado_assistant")
    poll_seconds = int(opts.get("poll_seconds", 60))

    auth = load_json(AUTH_FILE, {})
    refresh_token = auth.get("refresh_token")
    home_id = auth.get("home_id")

    if not refresh_token or not home_id:
        print("[worker] auth fehlt (refresh_token/home_id)")
        while True:
            time.sleep(5)

    client = mqtt_connect(opts)
    print("[worker] mqtt connected")

    discovery_done = False

    while True:
        try:
            access, refresh_token_new = refresh_tokens(refresh_token)

            # Refresh-Token Rotation: speichern!
            if refresh_token_new != refresh_token:
                auth["refresh_token"] = refresh_token_new
                auth["saved"] = int(time.time())
                write_json(AUTH_FILE, auth)
                refresh_token = refresh_token_new
                print("[worker] refresh_token rotated+saved")

            devices = fetch_mobile_devices(access, int(home_id))

            if not discovery_done:
                publish_discovery(client, disc_prefix, base_topic, int(home_id), devices)
                print(f"[worker] discovery published ({len(devices)} devices)")
                discovery_done = True

            publish_states(client, base_topic, devices)
            print(f"[worker] states published ({len(devices)} devices)")

        except Exception as e:
            # Bei AUTH_INVALID: du musst in der UI einmal Reset+Login machen
            msg = str(e)
            if msg.startswith("AUTH_INVALID:"):
                print("[worker] AUTH INVALID -> bitte in Web UI: Reset + Login neu")
            else:
                print(f"[worker] error: {e}")

        time.sleep(poll_seconds)


if __name__ == "__main__":
    main()
