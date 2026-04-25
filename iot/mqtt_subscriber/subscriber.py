"""
iot/mqtt_subscriber/subscriber.py

Persistent MQTT subscriber process.
Runs in its own Docker container (separate from FastAPI).
Subscribes to all depot temperature sensor topics.

Topic format: flowsync/{depot_id}/temperature
Payload:      {"device_id": "ESP001", "temp": 5.8, "ts": 1714000000}

This process must never crash silently.
On any MQTT reconnect, it re-subscribes to all topics.

Run with:
    python -m iot.mqtt_subscriber.subscriber
"""

import asyncio
import json
import logging
import os
import signal
import sys
from datetime import datetime

import paho.mqtt.client as mqtt
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from iot.mqtt_subscriber.excursion_detector import check_excursion

logging.basicConfig(
    level  = logging.INFO,
    format = "%(asctime)s | %(name)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("flowsync.iot.subscriber")

# ── Config from environment ───────────────────────────────────────────────────
MQTT_HOST    = os.getenv("MQTT_BROKER_HOST", "localhost")
MQTT_PORT    = int(os.getenv("MQTT_BROKER_PORT", "1883"))
MQTT_TOPIC   = "flowsync/+/temperature"   # + = wildcard for depot_id
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://flowsync:flowsync@localhost:5432/flowsync"
)

# Sync DB session for this persistent process
engine  = create_engine(DATABASE_URL, pool_pre_ping=True)
Session = sessionmaker(bind=engine)


def on_connect(client, userdata, flags, rc):
    """Called when MQTT broker connection is established."""
    if rc == 0:
        logger.info(f"Connected to MQTT broker at {MQTT_HOST}:{MQTT_PORT}")
        client.subscribe(MQTT_TOPIC, qos=1)
        logger.info(f"Subscribed to topic: {MQTT_TOPIC}")
    else:
        logger.error(f"MQTT connection failed — return code {rc}")


def on_disconnect(client, userdata, rc):
    """Called on unexpected disconnect. paho auto-reconnects."""
    if rc != 0:
        logger.warning(
            f"Unexpected MQTT disconnect (rc={rc}). "
            "Auto-reconnect will trigger."
        )


def on_message(client, userdata, msg):
    """
    Called for each received MQTT message.
    Runs synchronously — fast processing only.
    """
    try:
        # Extract depot_id from topic: flowsync/{depot_id}/temperature
        parts    = msg.topic.split("/")
        depot_id = parts[1] if len(parts) >= 3 else None

        if not depot_id:
            logger.warning(f"Cannot parse depot_id from topic: {msg.topic}")
            return

        # Parse payload
        payload = json.loads(msg.payload.decode("utf-8"))

        device_id  = payload.get("device_id", "unknown")
        temp_value = float(payload.get("temp", 0.0))
        ts_raw     = payload.get("ts")

        timestamp = (
            datetime.fromtimestamp(int(ts_raw))
            if ts_raw else datetime.utcnow()
        )

        logger.info(
            f"IoT reading: depot={depot_id} "
            f"device={device_id} temp={temp_value}°C"
        )

        # Process in DB session
        with Session() as db:
            _process_reading(
                db        = db,
                depot_id  = depot_id,
                device_id = device_id,
                temp      = temp_value,
                timestamp = timestamp,
            )

    except json.JSONDecodeError as e:
        logger.error(f"Invalid JSON payload on {msg.topic}: {e}")
    except Exception as e:
        logger.error(f"Message processing failed: {e}")


def _process_reading(
    db:        object,
    depot_id:  str,
    device_id: str,
    temp:      float,
    timestamp: datetime,
) -> None:
    """
    Write temperature reading to DB.
    Check excursion threshold.
    Update device heartbeat.
    """
    import uuid

    # Check per-product thresholds via excursion_detector
    is_excursion, temp_min, temp_max = check_excursion(
        db        = db,
        depot_id  = depot_id,
        device_id = device_id,
        temp      = temp,
    )

    # Write to temperature_logs (INSERT-only)
    log_id = str(uuid.uuid4())
    db.execute(
        text("""
            INSERT INTO temperature_logs
                (id, depot_id, reading_value, reading_type,
                 is_excursion,
                 excursion_threshold_min,
                 excursion_threshold_max,
                 device_id, logged_at)
            VALUES
                (:id, :depot_id, :reading_value, 'SENSOR',
                 :is_excursion, :excursion_threshold_min, :excursion_threshold_max,
                 :device_id, :logged_at)
        """),
        {
            "id": log_id,
            "depot_id": depot_id,
            "reading_value": temp,
            "is_excursion": is_excursion,
            "excursion_threshold_min": temp_min,
            "excursion_threshold_max": temp_max,
            "device_id": device_id,
            "logged_at": timestamp,
        },
    )

    # Update last_seen_at on IoT device
    db.execute(
        text("""
            UPDATE iot_devices
            SET last_seen_at = :last_seen_at
            WHERE device_id = :device_id
        """),
        {"last_seen_at": timestamp, "device_id": device_id},
    )

    db.commit()

    if is_excursion:
        logger.warning(
            f"EXCURSION DETECTED: depot={depot_id} "
            f"device={device_id} temp={temp}°C "
            f"range=[{temp_min},{temp_max}]"
        )
        _queue_excursion_alert(db, depot_id, device_id, temp, temp_min, temp_max)


def _queue_excursion_alert(
    db:       object,
    depot_id: str,
    device_id: str,
    temp:     float,
    temp_min: float,
    temp_max: float,
) -> None:
    """
    Queue WhatsApp notification for cold chain excursion.
    Actual sending handled by notification_service.
    """
    import json as _json
    import uuid
    nid = str(uuid.uuid4())

    db.execute(
        text("""
            INSERT INTO notifications
                (id, depot_id, channel, notification_type,
                 payload, status, sent_at)
            VALUES
                (:id, :depot_id, 'WHATSAPP', 'COLD_CHAIN_EXCURSION',
                 :payload, 'QUEUED', NOW())
        """),
        {
            "id": nid,
            "depot_id": depot_id,
            "payload": _json.dumps({
                "message": (
                    f"🚨 COLD CHAIN EXCURSION\n"
                    f"Sensor: {device_id}\n"
                    f"Temp: {temp}°C (safe: {temp_min}–{temp_max}°C)\n"
                    f"Immediate action required."
                ),
                "device_id": device_id,
                "temp": temp,
            }),
        },
    )
    db.commit()


def main():
    """Entry point — start MQTT subscriber loop."""
    client = mqtt.Client(
        client_id  = "flowsync-iot-subscriber",
        clean_session = False,   # persist subscriptions across reconnects
    )

    client.on_connect    = on_connect
    client.on_disconnect = on_disconnect
    client.on_message    = on_message

    # Graceful shutdown on SIGTERM (Docker stop)
    def _shutdown(sig, frame):
        logger.info("Shutdown signal received — disconnecting")
        client.disconnect()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT,  _shutdown)

    logger.info(f"Connecting to MQTT broker {MQTT_HOST}:{MQTT_PORT}")

    client.connect(
        MQTT_HOST,
        MQTT_PORT,
        keepalive = 60,
    )

    # Blocking loop — handles reconnects automatically
    client.loop_forever()


if __name__ == "__main__":
    main()