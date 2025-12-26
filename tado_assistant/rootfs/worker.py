import os
import json
import time
import logging
from pathlib import Path

import requests

try:
    import paho.mqtt.client as mqtt
except Exception:
    mqtt = None

DATA_DIR = Path("/data")
AUTH_FILE = DATA_DIR / "tado_auth.json"

TOKEN_URL = "https://login.tado.com/oauth2/token"
CLIENT_ID = "1bb50063-6b0c-4d11-bd99-387f4a91cc46"

API_BASE = "https://my.tado.com/api/v2"

LOG_LEVEL = os.getenv("LOG_LEVEL", "info").lower()
logging.basicConfig(level=getattr(logging, LOG_LEVEL.upper(), logging.INFO))
log = logging.getLogger("tado_assistant_worker")

MQTT_ENABLED = str(os.getenv("MQTT_ENABLED", "false")).lower() == "true"
MQTT_HOST = os.getenv("MQTT_HOST", "")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883") or "1883")
MQTT_USERNAME = os.getenv("MQTT_USERNAME", "")
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD", "")
MQTT_DISCOVERY_PREFIX = os.getenv("MQTT_DISCOVERY_PREFIX", "homeassistant")
MQTT_TOPIC_PREFIX = os.getenv("MQTT_TOPIC_PREFIX", "tado_assistant")


def load_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path: Path, data):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def refresh_access_token(auth: dict) -> dict:
    if not auth.get("refresh_token"):
        return auth

    # valid?
    if auth.get("access_token") and time.time() < float(auth.get("expires_at", 0) or 0) - 30:
        return auth

    r = requests.post(
        TOKEN_URL,
        data={
            "client_id": CLIENT_ID,
            "grant_type": "refresh_token",
            "refresh_token": auth["refresh_token"],
        },
        timeout=20,
    )
    if r.status_code >= 400:
        log.error("Refresh token failed: %s %s", r.status_code, r.text)
        return auth

    tok = r.json()
    auth["access_token"] = tok.get("access_token")
    auth["expires_at"] = time.time() + int(tok.get("expires_in", 0) or 0)
    save_json(AUTH_FILE, auth)
    log.info("Access token refreshed")
    return auth


def api_get(path: str, auth: dict):
    auth = refresh_access_token(auth)
    token = auth.get("access_token")
    if not token:
        return None, auth

    r = requests.get(
        f"{API_BASE}{path}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=25,
    )
    if r.status_code >= 400:
        log.error("API GET %s failed: %s %s", path, r.status_code, r.text[:200])
        return None, auth
    return r.json(), auth


class MqttPub:
    def __init__(self):
        self.client = None
        self.connected = False

    def connect(self):
        if not MQTT_ENABLED:
            return
        if mqtt is None:
            log.error("paho-mqtt not installed")
            return
        if not MQTT_HOST:
            log.error("MQTT enabled but mqtt.host is empty")
            return

        self.client = mqtt.Client()
        if MQTT_USERNAME:
            self.client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)

        def on_connect(client, userdata, flags, rc):
            self.connected = (rc == 0)
            log.info("MQTT connect rc=%s", rc)

        self.client.on_connect = on_connect
        self.client.connect(MQTT_HOST, MQTT_PORT, 60)
        self.client.loop_start()

        # wait a bit
        t0 = time.time()
        while time.time() - t0 < 5 and not self.connected:
            time.sleep(0.1)

    def publish(self, topic: str, payload, retain: bool = True):
        if not (MQTT_ENABLED and self.client and self.connected):
            return
        if isinstance(payload, (dict, list)):
            payload = json.dumps(payload, ensure_ascii=False)
        self.client.publish(topic, payload, retain=retain)

    def close(self):
        if self.client:
            try:
                self.client.loop_stop()
                self.client.disconnect()
            except Exception:
                pass


def mqtt_discovery_device(home_id: int):
    return {
        "identifiers": ["tado_assistant", str(home_id)],
        "name": "Tado Assistant",
        "manufacturer": "tado° (via Add-on)",
        "model": "Tado Assistant",
    }


def publish_discovery(pub: MqttPub, home_id: int, mobile_devices: list):
    # device_tracker per mobile device
    for d in mobile_devices:
        dev_id = d.get("id")
        name = d.get("name") or f"Device {dev_id}"
        object_id = f"tado_{dev_id}"
        unique_id = f"tado_assistant_tracker_{home_id}_{dev_id}"

        state_topic = f"{MQTT_TOPIC_PREFIX}/home_{home_id}/mobile/{dev_id}/state"

        cfg = {
            "name": f"Tado {name}",
            "unique_id": unique_id,
            "state_topic": state_topic,
            "payload_home": "home",
            "payload_not_home": "not_home",
            "source_type": "gps",
            "device": mqtt_discovery_device(home_id),
        }

        cfg_topic = f"{MQTT_DISCOVERY_PREFIX}/device_tracker/{object_id}/config"
        pub.publish(cfg_topic, cfg, retain=True)

    # sensor count home
    count_state = f"{MQTT_TOPIC_PREFIX}/home_{home_id}/anyone_home_count/state"
    cfg2 = {
        "name": "Tado Anyone Home (Count)",
        "unique_id": f"tado_assistant_anyone_home_count_{home_id}",
        "state_topic": count_state,
        "device": mqtt_discovery_device(home_id),
        "icon": "mdi:home-account",
    }
    pub.publish(f"{MQTT_DISCOVERY_PREFIX}/sensor/tado_anyone_home_count/config", cfg2, retain=True)


def publish_states(pub: MqttPub, home_id: int, mobile_devices: list):
    count_home = 0
    for d in mobile_devices:
        dev_id = d.get("id")
        # API returns something like: {"location":{"atHome":true/false}} (varies by firmware)
        at_home = None
        loc = d.get("location") or {}
        if "atHome" in loc:
            at_home = bool(loc.get("atHome"))
        elif "at_home" in loc:
            at_home = bool(loc.get("at_home"))

        # fallback: some APIs have "settings":{"geoTrackingEnabled":...} not presence; if missing -> unknown
        if at_home is True:
            count_home += 1

        if at_home is None:
            # publish nothing -> HA stays unknown; so publish not_home only if clearly false
            continue

        state = "home" if at_home else "not_home"
        state_topic = f"{MQTT_TOPIC_PREFIX}/home_{home_id}/mobile/{dev_id}/state"
        pub.publish(state_topic, state, retain=True)

    pub.publish(f"{MQTT_TOPIC_PREFIX}/home_{home_id}/anyone_home_count/state", str(count_home), retain=True)


def ensure_home_id(auth: dict) -> dict:
    if auth.get("home_id"):
        return auth

    me, auth = api_get("/me", auth)
    if not me:
        return auth

    # Try to find first home id
    # Common formats: me["homes"][0]["id"] OR me["homes"][0]["homeId"]
    hid = None
    homes = me.get("homes") if isinstance(me, dict) else None
    if isinstance(homes, list) and homes:
        h0 = homes[0]
        hid = h0.get("id") or h0.get("homeId")

    if hid:
        auth["home_id"] = int(hid)
        save_json(AUTH_FILE, auth)
        log.info("Home ID detected: %s", hid)
    return auth


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    pub = MqttPub()
    if MQTT_ENABLED:
        pub.connect()

    discovery_sent = False

    while True:
        auth = load_json(AUTH_FILE, default={}) or {}
        if not auth.get("refresh_token"):
            log.info("Not authenticated yet. Waiting...")
            time.sleep(10)
            continue

        auth = ensure_home_id(auth)
        home_id = auth.get("home_id")
        if not home_id:
            time.sleep(10)
            continue

        devices, auth = api_get(f"/homes/{home_id}/mobileDevices", auth)
        if not isinstance(devices, list):
            time.sleep(15)
            continue

        if MQTT_ENABLED and pub.connected:
            if not discovery_sent:
                publish_discovery(pub, home_id, devices)
                discovery_sent = True

            # IMPORTANT: publish states -> otherwise HA shows "Unbekannt"
            publish_states(pub, home_id, devices)

        time.sleep(30)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log.exception("Worker crashed: %s", e)
        raise
