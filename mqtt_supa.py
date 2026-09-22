import json
import logging
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone

import paho.mqtt.client as mqtt
import requests
from dotenv import load_dotenv

# --------------------------------------------------
# Configuration
# --------------------------------------------------

load_dotenv()

MQTT_HOST          = os.getenv("MQTT_HOST", "127.0.0.1")
MQTT_PORT          = int(os.getenv("MQTT_PORT", "1883"))
MQTT_USERNAME      = os.getenv("MQTT_USERNAME")
MQTT_PASSWORD      = os.getenv("MQTT_PASSWORD")
MQTT_TOPIC         = os.getenv("MQTT_TOPIC", "#")
MQTT_CLIENT_ID     = os.getenv("MQTT_CLIENT_ID", "supabase-ingestor")

SUPABASE_URL       = os.getenv("SUPABASE_URL")
SUPABASE_KEY       = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
SUPABASE_TABLE     = os.getenv("SUPABASE_TABLE", "mqtt_messages")

BUFFER_SIZE        = int(os.getenv("BUFFER_SIZE", "20"))
FLUSH_INTERVAL     = int(os.getenv("FLUSH_INTERVAL", "5"))

# False  — urgent messages are prepended to the buffer (default, small-scale).
# True   — urgent messages bypass the buffer and are sent on their own thread.
#          Use when message throughput is high and buffer delay is unacceptable.
URGENT_SKIP_BUFFER = os.getenv("URGENT_SKIP_BUFFER", "false").lower() == "true"

# Topics to ignore entirely. Edit this list to suit your setup.
# Example: EXCLUDED_TOPICS = ["z2m/bridge/logging", "z2m/bridge/state"]
EXCLUDED_TOPICS    = []

# SQLite file for messages that could not reach Supabase.
# Run retry_failed.py manually to replay them.
FAILED_DB          = os.getenv("FAILED_DB", "failed_messages.db")

# --------------------------------------------------
# Logging
# --------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

logger = logging.getLogger(__name__)

# --------------------------------------------------
# Validation
# --------------------------------------------------

if not SUPABASE_URL:
    raise RuntimeError("SUPABASE_URL is not configured")

if not SUPABASE_KEY:
    raise RuntimeError("SUPABASE_KEY is not configured")

if not MQTT_USERNAME:
    raise RuntimeError("MQTT_USERNAME is not configured")

if not MQTT_PASSWORD:
    raise RuntimeError("MQTT_PASSWORD is not configured")

# --------------------------------------------------
# Failed messages — SQLite
# --------------------------------------------------

_fdb      = sqlite3.connect(FAILED_DB, check_same_thread=False)
_fdb_lock = threading.Lock()

_fdb.execute("""
    CREATE TABLE IF NOT EXISTS failed_messages (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        failed_at   TEXT NOT NULL,
        received_at TEXT NOT NULL,
        topic       TEXT NOT NULL,
        payload     TEXT NOT NULL
    )
""")
_fdb.commit()

logger.info("Failed messages DB: %s", FAILED_DB)
logger.info("Urgent skip buffer: %s", URGENT_SKIP_BUFFER)


def save_failed(messages):
    """Write messages that could not reach Supabase to the local SQLite file."""
    failed_at = datetime.now(timezone.utc).isoformat()
    try:
        with _fdb_lock:
            _fdb.executemany(
                "INSERT INTO failed_messages (failed_at, received_at, topic, payload)"
                " VALUES (?, ?, ?, ?)",
                [
                    (failed_at, m["received_at(UTC)"], m["topic"], json.dumps(m["payload"]))
                    for m in messages
                ],
            )
            _fdb.commit()
        logger.warning("Saved %d failed message(s) to %s", len(messages), FAILED_DB)
    except sqlite3.Error as exc:
        logger.error("Could not write to failed messages DB: %s", exc)

# --------------------------------------------------
# Supabase
# --------------------------------------------------

SUPABASE_ENDPOINT = f"{SUPABASE_URL.rstrip('/')}/rest/v1/{SUPABASE_TABLE}"

SUPABASE_HEADERS = {
    "apikey":        SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type":  "application/json",
    "Prefer":        "return=minimal",
}

http = requests.Session()
http.headers.update(SUPABASE_HEADERS)


def send_to_supabase(messages):
    """Send one or more messages to Supabase."""
    if not messages:
        return True
    try:
        response = http.post(SUPABASE_ENDPOINT, json=messages, timeout=10)
        if response.ok:
            logger.info("Sent %d message(s) to Supabase", len(messages))
            return True
        logger.error("Supabase error %s: %s", response.status_code, response.text[:500])
    except requests.RequestException as exc:
        logger.error("Supabase connection error: %s", exc)
    return False

# --------------------------------------------------
# Buffer
# --------------------------------------------------

buffer      = []
buffer_lock = threading.Lock()
_wakeup     = threading.Event()


def add_to_buffer(message, front=False):
    with buffer_lock:
        if front:
            buffer.insert(0, message)
        else:
            buffer.append(message)
        should_flush = len(buffer) >= BUFFER_SIZE

    if should_flush:
        _wakeup.set()


def flush_buffer():
    with buffer_lock:
        if not buffer:
            return
        messages = buffer.copy()
        buffer.clear()

    if not send_to_supabase(messages):
        save_failed(messages)


def buffer_worker():
    """Flush normal buffer on timer or when full."""
    while True:
        _wakeup.wait(timeout=FLUSH_INTERVAL)
        _wakeup.clear()
        try:
            flush_buffer()
        except Exception as exc:
            logger.error("Buffer worker error: %s", exc)

# --------------------------------------------------
# Urgent — direct send on own thread (URGENT_SKIP_BUFFER=true)
# --------------------------------------------------

def send_urgent_direct(message):
    """Send a single urgent message immediately on its own thread."""
    if not send_to_supabase([message]):
        save_failed([message])

# --------------------------------------------------
# Message classification
# --------------------------------------------------

def is_urgent(payload):
    if not isinstance(payload, dict):
        return False
    if payload.get("occupancy") is True:
        return True
    if payload.get("contact") is False:   # False = door/window opened
        return True
    if payload.get("tamper") is True:
        return True
    if payload.get("water_leak") is True:
        return True
    return False

# --------------------------------------------------
# MQTT callbacks
# --------------------------------------------------

def on_connect(client, userdata, flags, reason_code, properties):
    if reason_code != 0:
        logger.error("MQTT connection failed: %s", reason_code)
        return
    logger.info("Connected to MQTT broker %s:%s", MQTT_HOST, MQTT_PORT)
    client.subscribe(MQTT_TOPIC)
    logger.info("Subscribed to MQTT topic: %s", MQTT_TOPIC)


def remove_nulls(value):
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, dict):
        return {k: remove_nulls(v) for k, v in value.items()}
    if isinstance(value, list):
        return [remove_nulls(v) for v in value]
    return value


def on_message(client, userdata, msg):
    try:
        _handle_message(msg)
    except Exception as exc:
        logger.error("Unhandled error processing %s: %s", msg.topic, exc)


def _handle_message(msg):
    received_at = datetime.now(timezone.utc).isoformat()

    if msg.topic in EXCLUDED_TOPICS:
        return

    try:
        payload = json.loads(msg.payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        logger.warning("Ignoring non-JSON MQTT message: %s", msg.topic)
        return

    payload = remove_nulls(payload)

    message = {
        "received_at(UTC)": received_at,
        "topic":            msg.topic,
        "payload":          payload,
    }

    if is_urgent(payload):
        logger.info("URGENT: %s -> %s", msg.topic, payload)
        if URGENT_SKIP_BUFFER:
            threading.Thread(target=send_urgent_direct, args=(message,), daemon=True).start()
        else:
            add_to_buffer(message, front=True)
        return

    add_to_buffer(message)


def on_disconnect(client, userdata, disconnect_flags, reason_code, properties):
    if reason_code != 0:
        logger.warning("Unexpected disconnect: %s", reason_code)

# --------------------------------------------------
# MQTT client
# --------------------------------------------------

def create_mqtt_client():
    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=MQTT_CLIENT_ID,
    )
    client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)
    client.on_connect    = on_connect
    client.on_message    = on_message
    client.on_disconnect = on_disconnect
    return client

# --------------------------------------------------
# Main
# --------------------------------------------------

def main():
    logger.info("Starting MQTT → Supabase")

    threading.Thread(target=buffer_worker, daemon=True).start()

    client = create_mqtt_client()

    while True:
        try:
            logger.info("Connecting to MQTT %s:%s...", MQTT_HOST, MQTT_PORT)
            client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
            client.loop_forever()

        except KeyboardInterrupt:
            logger.info("Stopping...")
            break

        except Exception as exc:
            logger.error("MQTT error: %s", exc)
            time.sleep(5)

    flush_buffer()
    http.close()
    _fdb.close()


if __name__ == "__main__":
    main()