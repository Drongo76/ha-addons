import os
import json
import time
import logging
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import requests

try:
    import paho.mqtt.client as mqtt  # type: ignore
except Exception:
    mqtt = None

log = logging.getLogger("tado_worker")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))

DATA_DIR = Path("/data")
AUTH_FILE = DATA_DIR / "tado_auth.json"

TOKEN_URL = "https://login.tado.com/oauth2/token"
CLIENT_ID = "1bb50063-6b0c-4d11-bd99-387f4a91cc46"
API_BASE = "https://my.tado.com/api/v2"

POLL_SECONDS = int(os.getenv("POLL_SECONDS", "30") or "30")

MQTT_ENABLED = str(os.getenv("MQTT_ENABLED", "false")).lower() == "true"
MQTT_HOST = os.getenv("MQTT_HOST", "")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883") or "1883")
MQTT_USERNAME = os.getenv("MQTT_USERNAME", "")
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD", "")
MQTT_DISCOVERY_PREFIX = os.getenv("MQTT_DISCOVERY_PREFIX", "homeassistant")
MQTT_TOPIC_PREFIX = os.getenv("MQTT_TOPIC_PREFIX", "tado_assistant")


def load_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _invalidate_auth(auth: Dict[str, Any], clear_refresh: bool) -> Dict[str, Any]:
    auth.pop("access_token", None)
    auth.pop("expires_at", None)
    if clear_refresh:
        auth.pop("refresh_token", None)
    save_json(AUTH_FILE, auth)
    return auth


def refresh_access_token(auth: Dict[str, Any]) -> Dict[str, Any]:
    """
    Refreshes access token using refresh_token.
    Never raises. If refresh token is invalid -> clears it so UI can re-auth.
    """
    refresh_token = auth.get("refresh_token")
    if not refresh_token:
        return auth

    # Access token still valid?
    if auth.get("access_token") and time.time() < float(auth.get("expires_at", 0) or 0) - 30:
        return auth

    try:
        r = requests.post(
            TOKEN_URL,
            data={
                "client_id": CLIENT_ID,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
            timeout=20,
        )
    except Exception as e:
        log.error("Token refresh request failed: %s", e)
        return auth

    if r.status_code >= 400:
        body = (r.text or "")[:500]
        # Typical cases: invalid_grant / refresh_token_not_found
        clear_refresh = "invalid_grant" in body or "refresh_token" in body
        log.error("Token refresh failed (%s): %s", r.status_code, body)
        if clear_refresh:
            log.error("Refresh token invalid/missing -> login required. Clearing refresh_token.")
            auth = _invalidate_auth(auth, clear_refresh=True)
        else:
            auth = _invalidate_auth(auth, clear_refresh=False)
        return auth

    try:
        tok = r.json()
    except Exception:
        log.error("Token refresh response not JSON: %s", (r.text or "")[:200])
        return auth

    auth["access_token"] = tok.get("access_token")
    auth["expires_at"] = time.time() + int(tok.get("expires_in", 0) or 0)
    if tok.get("refresh_token"):
        auth["refresh_token"] = tok.get("refresh_token")
    save_json(AUTH_FILE, auth)
    log.info("Access token refreshed")
    return auth


def api_get(path: str, auth: Dict[str, Any]) -> Tuple[Optional[Any], Dict[str, Any]]:
    auth = refresh_access_token(auth)
    token = auth.get("access_token")
    if not token:
        return None, auth

    try:
        r = requests.get(
            f"{API_BASE}{path}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=25,
        )
    except Exception as e:
        log.error("API GET %s failed: %s", path, e)
        return None, auth

    if r.status_code == 401:
        log.error("API GET %s unauthorized (401). Clearing access token.", path)
        auth = _invalidate_auth(auth, clear_refresh=False)
        return None, auth

    if r.status_code >= 400:
        log.error("API GET %s failed: %s %s", path, r.status_code, (r.text or "")[:200])
        return None, auth

    try:
        return r.json(), auth
    except Exception:
        log.error("API GET %s returned non-JSON: %s", path, (r.text or "")[:200])
        return None, auth


class MqttPub:
    def __init__(self) -> None:
        self.client = None
        self.connected = False

    def connect(self) -> None:
        if not MQTT_ENABLED:
            return
        if mqtt is None:
            log.error("MQTT enabled but paho-mqtt is not installed")
            return
        if not MQTT_HOST:
            log.error("MQTT enabled but MQTT_HOST is empty")
            return

        self.client = mqtt.Client()
        if MQTT_USERNAME:
            self.client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)

        def on_connect(client, userdata, flags, rc):
            self.connected = (rc == 0)
            if self.connected:
                log.info("[worker] mqtt connected")
            else:
                log.error("[worker] mqtt connect failed rc=%s", rc)

        def on_disconnect(client, userdata, rc):
            self.connected = False
            log.error("[worker] mqtt disconnected rc=%s", rc)

        self.client.on_connect = on_connect
        self.client.on_disconnect = on_disconnect

        try:
            self.client.connect(MQTT_HOST, MQTT_PORT, 60)
            self.client.loop_start()
        except Exception as e:
            log.error("MQTT connect error: %s", e)
            self.client = None
            self.connected = False

    def publish(self, topic: str, payload: str, retain: bool = False) -> None:
        if not (MQTT_ENABLED and self.client and self.connected):
            return
        try:
            self.client.publish(topic, payload, retain=retain)
        except Exception as e:
            log.error("MQTT publish failed: %s", e)
            self.connected = False


def mqtt_discovery_device(home_id: int) -> Dict[str, Any]:
    return {
        "identifiers": ["tado_assistant", str(home_id)],
        "name": "Tado Assistant",
        "manufacturer": "tado° (via Add-on)",
        "model": "Tado Assistant",
    }


def publish_discovery(pub: MqttPub, home_id: int, mobile_devices: list) -> None:
    for d in mobile_devices:
        dev_id = d.get("id")
        name = d.get("name") or f"Device {dev_id}"
        object_id = f"tado_{dev_id}"
        unique_id = f"tado_assistant_tracker_{home_id}_{dev_id}"
        state_topic = f"{MQTT_TOPIC_PREFIX}/home_{home_id}/mobile/{dev_id}/state"

        cfg = {
            "name": f"Tado {name}",
            "uniq_id": unique_id,
            "stat_t": state_topic,
            "pl_home": "home",
            "pl_not_home": "not_home",
            "dev": mqtt_discovery_device(home_id),
        }
        cfg_topic = f"{MQTT_DISCOVERY_PREFIX}/device_tracker/{object_id}/config"
        pub.publish(cfg_topic, json.dumps(cfg), retain=True)

    count_state = f"{MQTT_TOPIC_PREFIX}/home_{home_id}/anyone_home_count/state"
    cfg2 = {
        "name": "Tado Anyone Home Count",
        "uniq_id": f"tado_assistant_anyone_home_count_{home_id}",
        "stat_t": count_state,
        "dev": mqtt_discovery_device(home_id),
    }
    pub.publish(f"{MQTT_DISCOVERY_PREFIX}/sensor/tado_anyone_home_count/config", json.dumps(cfg2), retain=True)


def publish_states(pub: MqttPub, home_id: int, mobile_devices: list) -> None:
    count_home = 0
    for d in mobile_devices:
        dev_id = d.get("id")

        at_home = None
        loc = d.get("location") or {}
        if "atHome" in loc:
            at_home = bool(loc.get("atHome"))
        elif "at_home" in loc:
            at_home = bool(loc.get("at_home"))

        if at_home is True:
            count_home += 1

        if at_home is None:
            continue

        state = "home" if at_home else "not_home"
        state_topic = f"{MQTT_TOPIC_PREFIX}/home_{home_id}/mobile/{dev_id}/state"
        pub.publish(state_topic, state, retain=True)

    pub.publish(f"{MQTT_TOPIC_PREFIX}/home_{home_id}/anyone_home_count/state", str(count_home), retain=True)


def ensure_home_id(auth: Dict[str, Any]) -> Dict[str, Any]:
    if auth.get("home_id"):
        return auth

    me, auth = api_get("/me", auth)
    if not me:
        return auth

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


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    pub = MqttPub()
    if MQTT_ENABLED:
        pub.connect()

    discovery_sent = False

    while True:
        try:
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

            if MQTT_ENABLED and (not pub.connected):
                pub.connect()

            devices, auth = api_get(f"/homes/{home_id}/mobileDevices", auth)
            if not isinstance(devices, list):
                time.sleep(15)
                continue

            if MQTT_ENABLED and pub.connected:
                if not discovery_sent:
                    publish_discovery(pub, int(home_id), devices)
                    discovery_sent = True
                publish_states(pub, int(home_id), devices)

            time.sleep(POLL_SECONDS)

        except Exception as e:
            log.exception("Worker loop error: %s", e)
            time.sleep(10)


if __name__ == "__main__":
    log.info("[worker] start")
    main()
