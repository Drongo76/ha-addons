#!/usr/bin/env python3
import os
import json
import time
import logging
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, List

import requests

try:
    import paho.mqtt.client as mqtt  # type: ignore
except Exception:
    mqtt = None

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL)
log = logging.getLogger("tado_worker")

DATA_DIR = Path("/data")
AUTH_FILE = DATA_DIR / "tado_auth.json"
OPTIONS_FILE = Path("/data/options.json")  # HA Add-on options

TOKEN_URL = "https://login.tado.com/oauth2/token"
CLIENT_ID = "1bb50063-6b0c-4d11-bd99-387f4a91cc46"
API_BASE = "https://my.tado.com/api/v2"

DEFAULT_POLL_SECONDS = 30


def load_json(path: Path, default: Any) -> Any:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        log.error("Failed reading %s: %s", path, e)
    return default


def save_json(path: Path, data: Any) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        log.error("Failed writing %s: %s", path, e)


def load_options() -> Dict[str, Any]:
    # HA Add-on injects options.json under /data
    return load_json(OPTIONS_FILE, default={}) or {}


def load_auth() -> Dict[str, Any]:
    return load_json(AUTH_FILE, default={}) or {}


def save_auth(auth: Dict[str, Any]) -> None:
    save_json(AUTH_FILE, auth)


def auth_has_valid_access(auth: Dict[str, Any]) -> bool:
    access = auth.get("access_token")
    exp = float(auth.get("expires_at", 0) or 0)
    return bool(access) and (time.time() < exp - 30)


def refresh_access_token(auth: Dict[str, Any]) -> Dict[str, Any]:
    """
    Refresh token flow.
    Never raises. On invalid refresh token -> clears refresh_token so UI can re-login.
    """
    refresh_token = auth.get("refresh_token")
    if not refresh_token:
        return auth

    if auth_has_valid_access(auth):
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
        log.error("Token refresh failed (%s): %s", r.status_code, body)

        # Typical invalid refresh cases
        if "invalid_grant" in body or "refresh_token" in body or "not_found" in body:
            log.error("Refresh token invalid/missing -> login required. Clearing tokens.")
            auth.pop("access_token", None)
            auth.pop("expires_at", None)
            auth.pop("refresh_token", None)
            save_auth(auth)
        else:
            # Keep refresh_token, but invalidate access token
            auth.pop("access_token", None)
            auth.pop("expires_at", None)
            save_auth(auth)

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

    save_auth(auth)
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
        log.error("API GET %s unauthorized. Clearing access token.", path)
        auth.pop("access_token", None)
        auth.pop("expires_at", None)
        save_auth(auth)
        return None, auth

    if r.status_code >= 400:
        log.error("API GET %s failed: %s %s", path, r.status_code, (r.text or "")[:200])
        return None, auth

    try:
        return r.json(), auth
    except Exception:
        log.error("API GET %s returned non-JSON: %s", path, (r.text or "")[:200])
        return None, auth


def ensure_home_id(auth: Dict[str, Any]) -> Dict[str, Any]:
    if auth.get("home_id"):
        return auth

    me, auth = api_get("/me", auth)
    if not isinstance(me, dict):
        return auth

    homes = me.get("homes")
    if isinstance(homes, list) and homes:
        hid = homes[0].get("id") or homes[0].get("homeId")
        if hid:
            auth["home_id"] = int(hid)
            save_auth(auth)
            log.info("Home ID detected: %s", hid)

    return auth


class MqttPublisher:
    def __init__(self, cfg: Dict[str, Any]) -> None:
        self.cfg = cfg
        self.client = None
        self.connected = False

    def enabled(self) -> bool:
        return bool(self.cfg.get("mqtt_enabled", False) or os.getenv("MQTT_ENABLED", "false").lower() == "true")

    def _host(self) -> str:
        return str(self.cfg.get("mqtt_host") or os.getenv("MQTT_HOST", "")).strip()

    def _port(self) -> int:
        return int(self.cfg.get("mqtt_port") or os.getenv("MQTT_PORT", "1883") or 1883)

    def _user(self) -> str:
        return str(self.cfg.get("mqtt_username") or os.getenv("MQTT_USERNAME", "")).strip()

    def _pass(self) -> str:
        return str(self.cfg.get("mqtt_password") or os.getenv("MQTT_PASSWORD", "")).strip()

    def discovery_prefix(self) -> str:
        return str(self.cfg.get("mqtt_discovery_prefix") or os.getenv("MQTT_DISCOVERY_PREFIX", "homeassistant"))

    def topic_prefix(self) -> str:
        return str(self.cfg.get("mqtt_topic_prefix") or os.getenv("MQTT_TOPIC_PREFIX", "tado_assistant"))

    def connect(self) -> None:
        if not self.enabled():
            return
        if mqtt is None:
            log.error("MQTT enabled but paho-mqtt not installed")
            return

        host = self._host()
        if not host:
            log.error("MQTT enabled but mqtt_host/MQTT_HOST is empty")
            return

        self.client = mqtt.Client()
        user = self._user()
        if user:
            self.client.username_pw_set(user, self._pass())

        def on_connect(client, userdata, flags, rc):
            self.connected = (rc == 0)
            if self.connected:
                log.info("MQTT connected")
            else:
                log.error("MQTT connect failed rc=%s", rc)

        def on_disconnect(client, userdata, rc):
            self.connected = False
            log.error("MQTT disconnected rc=%s", rc)

        self.client.on_connect = on_connect
        self.client.on_disconnect = on_disconnect

        try:
            self.client.connect(host, self._port(), 60)
            self.client.loop_start()
        except Exception as e:
            log.error("MQTT connect error: %s", e)
            self.client = None
            self.connected = False

    def publish(self, topic: str, payload: str, retain: bool = False) -> None:
        if not (self.enabled() and self.client and self.connected):
            return
        try:
            self.client.publish(topic, payload, retain=retain)
        except Exception as e:
            log.error("MQTT publish failed: %s", e)
            self.connected = False


def mqtt_device(home_id: int) -> Dict[str, Any]:
    return {
        "identifiers": ["tado_assistant", str(home_id)],
        "name": "Tado Assistant",
        "manufacturer": "tado° (via HA Add-on)",
        "model": "Tado Assistant",
    }


def publish_discovery(pub: MqttPublisher, home_id: int, devices: List[Dict[str, Any]]) -> None:
    dp = pub.discovery_prefix()
    tp = pub.topic_prefix()

    for d in devices:
        dev_id = d.get("id")
        name = d.get("name") or f"Device {dev_id}"
        object_id = f"tado_mobile_{dev_id}"
        unique_id = f"tado_assistant_tracker_{home_id}_{dev_id}"

        state_topic = f"{tp}/home_{home_id}/mobile/{dev_id}/state"
        cfg = {
            "name": f"Tado {name}",
            "uniq_id": unique_id,
            "stat_t": state_topic,
            "pl_home": "home",
            "pl_not_home": "not_home",
            "dev": mqtt_device(home_id),
        }
        pub.publish(f"{dp}/device_tracker/{object_id}/config", json.dumps(cfg), retain=True)

    # count sensor
    count_topic = f"{tp}/home_{home_id}/anyone_home_count/state"
    cfg2 = {
        "name": "Tado Anyone Home Count",
        "uniq_id": f"tado_assistant_anyone_home_count_{home_id}",
        "stat_t": count_topic,
        "dev": mqtt_device(home_id),
    }
    pub.publish(f"{dp}/sensor/tado_anyone_home_count/config", json.dumps(cfg2), retain=True)


def publish_states(pub: MqttPublisher, home_id: int, devices: List[Dict[str, Any]]) -> None:
    tp = pub.topic_prefix()
    count_home = 0

    for d in devices:
        dev_id = d.get("id")
        loc = d.get("location") or {}

        # tado uses atHome, some wrappers use at_home
        at_home = None
        if "atHome" in loc:
            at_home = bool(loc.get("atHome"))
        elif "at_home" in loc:
            at_home = bool(loc.get("at_home"))

        if at_home is None:
            continue

        if at_home:
            count_home += 1

        state = "home" if at_home else "not_home"
        pub.publish(f"{tp}/home_{home_id}/mobile/{dev_id}/state", state, retain=True)

    pub.publish(f"{tp}/home_{home_id}/anyone_home_count/state", str(count_home), retain=True)


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    opts = load_options()
    poll_seconds = int(opts.get("poll_seconds", DEFAULT_POLL_SECONDS) or DEFAULT_POLL_SECONDS)

    pub = MqttPublisher(opts)
    if pub.enabled():
        pub.connect()

    discovery_sent = False

    while True:
        try:
            auth = load_auth()

            # Not logged in yet
            if not auth.get("refresh_token"):
                log.info("Not authenticated yet (no refresh_token). Waiting...")
                time.sleep(10)
                continue

            auth = ensure_home_id(auth)
            home_id = auth.get("home_id")
            if not home_id:
                time.sleep(10)
                continue

            if pub.enabled() and not pub.connected:
                pub.connect()

            devices, auth = api_get(f"/homes/{home_id}/mobileDevices", auth)
            if not isinstance(devices, list):
                time.sleep(15)
                continue

            if pub.enabled() and pub.connected:
                if not discovery_sent:
                    publish_discovery(pub, int(home_id), devices)
                    discovery_sent = True
                publish_states(pub, int(home_id), devices)

            time.sleep(poll_seconds)

        except Exception as e:
            log.exception("Worker loop error (staying alive): %s", e)
            time.sleep(10)


if __name__ == "__main__":
    log.info("worker start")
    main()
