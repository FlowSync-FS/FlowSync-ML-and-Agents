"""
ml/inference/infer_anomaly.py

AnomalyEngine — Z-score per product_category.
Called by orchestrator.py at Stage 4 as asyncio.create_task()
(background, non-blocking — does not delay agents).

MVP: Z-score statistical engine, no model file needed.
Month 6 upgrade: swap internals for Isolation Forest.
The agent layer never changes — it reads anomaly_flags regardless.

Thresholds read from compliance_config via config_loader.
Never hardcode 2.5 or 2.0 here.
"""

import logging
from datetime import date
from typing import Optional

import numpy as np
import pandas as pd

from ml.features.anomaly_features import score_movement_zscore
from ml.shared.config_loader import get

logger = logging.getLogger("flowsync.infer.anomaly")


async def run(
    depot_id: str,
    run_date: str,
    db,
) -> list:
    """
    Score yesterday's stock movements for anomalies.
    Writes flags to anomaly_flags table.
    Read by AnomalyAgent in Stage 5.

    Args:
        depot_id: UUID string
        run_date: ISO date string
        db:       async SQLAlchemy session

    Returns:
        List of flag dicts:
        [{
            movement_id, batch_id, z_score,
            action: 'ANOMALY_HOLD' | 'ANOMALY_ALERT',
            quantity, movement_type
        }]
    """
    hold_threshold  = get("anomaly_hold_threshold",  db, default=2.5)
    alert_threshold = get("anomaly_alert_threshold", db, default=2.0)

    # Build rolling 30-day baselines per product_category
    baseline_rows = await db.execute("""
        SELECT
            p.product_category,
            sm.quantity
        FROM stock_movements sm
        JOIN products p ON p.id = sm.product_id
        WHERE sm.depot_id    = :did
          AND sm.created_at >= NOW() - INTERVAL '30 days'
    """, {"did": depot_id})

    baseline_df = pd.DataFrame(
        baseline_rows.fetchall(),
        columns=["product_category", "quantity"],
    )

    # Compute per-category mean and std
    # One baseline per category — never global
    # Antibiotics and OTC vitamins have completely different distributions
    baselines: dict = {}
    if not baseline_df.empty:
        for cat, grp in baseline_df.groupby("product_category"):
            mean = float(grp["quantity"].mean())
            std  = float(grp["quantity"].std())
            if std == 0 or pd.isna(std):
                std = 1.0   # single-SKU category edge case
            baselines[str(cat)] = {"mean": mean, "std": std}

    if not baselines:
        logger.warning(
            f"[{depot_id}] No baseline data for anomaly detection — "
            "skipping (need 30 days of movement history)"
        )
        return []

    # Score movements from last 48 hours
    movement_rows = await db.execute("""
        SELECT
            sm.id::text             AS movement_id,
            sm.batch_id::text,
            sm.product_id::text,
            sm.quantity,
            sm.movement_type,
            sm.created_at,
            p.product_category
        FROM stock_movements sm
        JOIN products p ON p.id = sm.product_id
        WHERE sm.depot_id    = :did
          AND sm.created_at >= NOW() - INTERVAL '2 days'
    """, {"did": depot_id})

    movements = movement_rows.fetchall()

    if not movements:
        logger.info(f"[{depot_id}] No recent movements — anomaly skipped")
        return []

    flags = []

    for row in movements:
        z = score_movement_zscore(
            quantity=float(row.quantity),
            product_category=str(row.product_category),
            baselines=baselines,
        )

        if z > hold_threshold:
            action = "ANOMALY_HOLD"
        elif z > alert_threshold:
            action = "ANOMALY_ALERT"
        else:
            continue   # normal movement — skip

        flags.append({
            "movement_id":   row.movement_id,
            "batch_id":      row.batch_id,
            "product_id":    row.product_id,
            "z_score":       round(z, 2),
            "action":        action,
            "quantity":      float(row.quantity),
            "movement_type": row.movement_type,
        })

    hold_count  = sum(1 for f in flags if f["action"] == "ANOMALY_HOLD")
    alert_count = sum(1 for f in flags if f["action"] == "ANOMALY_ALERT")

    logger.info(
        f"[{depot_id}] Anomaly: {len(movements)} movements checked | "
        f"{hold_count} HOLDs | {alert_count} ALERTs"
    )

    await _write_flags(depot_id, run_date, flags, db)
    return flags


async def _write_flags(
    depot_id: str,
    run_date: str,
    flags: list,
    db,
) -> None:
    """
    Insert anomaly flags. Clears today's flags first to avoid duplicates
    when the pipeline is re-run on the same day.
    """
    await db.execute("""
        DELETE FROM anomaly_flags
        WHERE depot_id = :did AND run_date = :rd
    """, {"did": depot_id, "rd": run_date})

    for f in flags:
        await db.execute("""
            INSERT INTO anomaly_flags
                (depot_id, movement_id, batch_id, product_id, run_date,
                 z_score, action, quantity, movement_type)
            VALUES
                (:did, :mid, :bid, :pid, :rd,
                 :z, :action, :qty, :mtype)
        """, {
            "did":    depot_id,
            "mid":    f["movement_id"],
            "bid":    f["batch_id"],
            "pid":    f["product_id"],
            "rd":     run_date,
            "z":      f["z_score"],
            "action": f["action"],
            "qty":    f["quantity"],
            "mtype":  f["movement_type"],
        })

    await db.commit()


def detect_temperature_anomalies(
    readings: pd.DataFrame,
    hold_threshold: Optional[float] = None,
    alert_threshold: Optional[float] = None,
) -> pd.DataFrame:
    """
    Score temperature readings for cold-chain excursions.
    Pandas-only — no DB required. Called by orchestrator Stage 4
    as asyncio.create_task(asyncio.to_thread(detect_temperature_anomalies, df)).

    Args:
        readings:        DataFrame with columns:
                         device_id, depot_id, temperature_c,
                         timestamp, product_type
        hold_threshold:  Z-score above which action='ANOMALY_HOLD'
                         Defaults to config value (2.5).
        alert_threshold: Z-score above which action='ANOMALY_ALERT'
                         Defaults to config value (2.0).

    Returns:
        DataFrame with original columns plus:
            z_score (float), action (str or None), is_excursion (bool)
        Rows with no excursion still included (action=None, is_excursion=False).
    """
    hold_thresh  = hold_threshold  if hold_threshold  is not None else get("anomaly_hold_threshold",  default=2.5)
    alert_thresh = alert_threshold if alert_threshold is not None else get("anomaly_alert_threshold", default=2.0)

    df = readings.copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"])

    # Per-device rolling baseline: mean + std of temperature_c
    baselines: dict = {}
    for device_id, grp in df.groupby("device_id"):
        temps = grp["temperature_c"].dropna()
        mean  = float(temps.mean())
        std   = float(temps.std())
        if np.isnan(std) or std == 0:
            std = 1.0
        baselines[str(device_id)] = {"mean": mean, "std": std}

    def _zscore(row) -> float:
        b = baselines.get(str(row["device_id"]))
        if b is None:
            return 0.0
        return abs(float((row["temperature_c"] - b["mean"]) / b["std"]))

    df["z_score"] = df.apply(_zscore, axis=1)

    def _action(z: float) -> Optional[str]:
        if z > hold_thresh:
            return "ANOMALY_HOLD"
        if z > alert_thresh:
            return "ANOMALY_ALERT"
        return None

    df["action"]       = df["z_score"].apply(_action)
    df["is_excursion"] = df["action"].notna()

    excursions = int(df["is_excursion"].sum())
    logger.info(
        f"Temperature anomaly: {len(df)} readings checked | "
        f"{excursions} excursions detected"
    )
    return df