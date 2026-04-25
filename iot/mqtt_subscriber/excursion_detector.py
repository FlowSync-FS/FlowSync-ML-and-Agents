"""
iot/mqtt_subscriber/excursion_detector.py

Checks whether an IoT temperature reading is an excursion.
Reads per-device and per-product thresholds from DB.
Falls back to compliance_config defaults if no specific threshold found.

Separated from subscriber.py so it can be unit tested
independently of the MQTT loop.
"""

import logging
from typing import Optional, Tuple

from sqlalchemy import text

logger = logging.getLogger("flowsync.iot.excursion")

# Hardcoded defaults — used when DB is unreachable
DEFAULT_COLD_CHAIN_MIN =  2.0
DEFAULT_COLD_CHAIN_MAX =  8.0
DEFAULT_GENERAL_MIN    = 15.0
DEFAULT_GENERAL_MAX    = 25.0


def check_excursion(
    db:        object,
    depot_id:  str,
    device_id: str,
    temp:      float,
) -> Tuple[bool, float, float]:
    """
    Determine if a temperature reading is an excursion.

    Strategy:
        1. Find which product this device monitors (via iot_devices → fridge_label)
        2. Get product-specific thresholds from products table
        3. Fall back to compliance_config defaults
        4. Return (is_excursion, temp_min, temp_max)

    Args:
        db:        sync SQLAlchemy session (not async — MQTT is sync)
        depot_id:  UUID string
        device_id: device_id from MQTT payload
        temp:      temperature reading in °C

    Returns:
        (is_excursion, threshold_min, threshold_max)
    """
    temp_min, temp_max = _get_thresholds(db, depot_id, device_id)
    is_excursion       = temp < temp_min or temp > temp_max

    logger.debug(
        f"Excursion check: device={device_id} "
        f"temp={temp} range=[{temp_min},{temp_max}] "
        f"excursion={is_excursion}"
    )

    return is_excursion, temp_min, temp_max


def _get_thresholds(
    db:        object,
    depot_id:  str,
    device_id: str,
) -> Tuple[float, float]:
    """
    Lookup thresholds for a device.

    First: check if device monitors a specific cold-chain product.
    Then:  fall back to compliance_config defaults.
    """
    try:
        # Check if this device is linked to a cold-chain product batch
        row = db.execute(
            text("""
                SELECT p.storage_temp_min, p.storage_temp_max
                FROM iot_devices iot
                JOIN depots d ON d.id = iot.depot_id
                JOIN batches b ON b.depot_id = d.id
                JOIN products p ON p.id = b.product_id
                WHERE iot.device_id = :device_id
                  AND iot.depot_id  = :depot_id
                  AND p.is_cold_chain = TRUE
                  AND b.expiry_date > NOW()
                LIMIT 1
            """),
            {"device_id": device_id, "depot_id": depot_id},
        ).fetchone()

        if row and row.storage_temp_min is not None:
            return float(row.storage_temp_min), float(row.storage_temp_max)

        # Fall back to compliance_config
        config_row = db.execute(
            text("""
                SELECT key, value FROM compliance_config
                WHERE key IN (
                    'temp_cold_chain_min', 'temp_cold_chain_max'
                )
            """),
        ).fetchall()

        config = {r.key: float(r.value) for r in config_row}

        return (
            config.get("temp_cold_chain_min", DEFAULT_COLD_CHAIN_MIN),
            config.get("temp_cold_chain_max", DEFAULT_COLD_CHAIN_MAX),
        )

    except Exception as e:
        logger.warning(
            f"Threshold lookup failed for device={device_id}: {e}. "
            "Using hardcoded defaults."
        )
        return DEFAULT_COLD_CHAIN_MIN, DEFAULT_COLD_CHAIN_MAX