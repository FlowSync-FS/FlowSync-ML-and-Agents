"""
ml/inference/infer_expiry.py

ExpiryRiskModel production inference.
Called by orchestrator.py at Stage 2a (parallel with infer_stockout).

Uses ML model if depot has 180+ days data and 50+ expired-batch labels.
Falls back to deterministic formula until then.
Writes to expiry_predictions table.
"""

import logging
from datetime import date, timedelta

import pandas as pd

from ml.features.expiry_features import (
    build_expiry_features,
    compute_liquidation_date,
    FEATURE_COLS,
)
from ml.registry.model_store import ModelStore

logger = logging.getLogger("flowsync.infer.expiry")


async def run(
    depot_id: str,
    demand_results: dict,
    db,
) -> dict:
    """
    Run expiry risk scoring for all active batches in one depot.

    Args:
        depot_id:       UUID string
        demand_results: output from infer_demand.run()
                        {product_id: {demand_trend_slope, predicted_daily_rate}}
        db:             async SQLAlchemy session

    Returns:
        {
            batch_id: {
                expiry_risk_score:            float (0.0-1.0),
                recommended_liquidation_date: str (ISO date),
                method:                       str ('model' or 'formula'),
            }
        }
    """
    # Fetch all active batches with remaining stock
    batch_rows = await db.execute("""
        SELECT
            b.id::text                          AS batch_id,
            b.product_id::text,
            b.depot_id::text,
            b.expiry_date,
            b.quantity_received,
            b.quantity_received
                - COALESCE(sold.qty, 0)         AS quantity_remaining,
            p.product_category,
            p.is_cold_chain,
            p.default_shelf_life_days,
            b.created_at
        FROM batches b
        JOIN products p ON p.id = b.product_id
        LEFT JOIN (
            SELECT batch_id, SUM(quantity) AS qty
            FROM   stock_movements
            WHERE  movement_type = 'OUT'
            GROUP  BY batch_id
        ) sold ON sold.batch_id = b.id
        WHERE b.depot_id   = :did
          AND b.expiry_date > NOW()
          AND (b.quantity_received - COALESCE(sold.qty, 0)) > 0
    """, {"did": depot_id})

    batches_df = pd.DataFrame(batch_rows.fetchall(), columns=[
        "batch_id", "product_id", "depot_id", "expiry_date",
        "quantity_received", "quantity_remaining",
        "product_category", "is_cold_chain",
        "default_shelf_life_days", "created_at",
    ])

    if batches_df.empty:
        logger.info(f"[{depot_id}] No active batches — skipping expiry")
        return {}

    # Build demand trend slope feature from demand_results
    demand_rows = [
        {
            "product_id":         pid,
            "depot_id":           depot_id,
            "demand_trend_slope": v.get("demand_trend_slope", 0.0),
            "predicted_daily_rate": v.get("predicted_daily_rate", 0.0),
        }
        for pid, v in demand_results.items()
    ]
    demand_df = pd.DataFrame(demand_rows) if demand_rows else pd.DataFrame()

    use_model = await _should_use_model(depot_id, db)
    results   = {}

    if use_model:
        try:
            results = await _run_model(
                batches_df, demand_df, depot_id, db
            )
            logger.info(
                f"[{depot_id}] Expiry: ML model scored "
                f"{len(results)} batches"
            )
        except Exception as e:
            logger.warning(
                f"[{depot_id}] Expiry model failed: {e} "
                "— falling back to formula"
            )
            use_model = False

    if not use_model:
        results = _run_formula(batches_df)
        logger.info(
            f"[{depot_id}] Expiry: formula scored "
            f"{len(results)} batches (cold start)"
        )

    await _write_predictions(depot_id, date.today().isoformat(), results, db)
    return results


async def _run_model(
    batches_df: pd.DataFrame,
    demand_df: pd.DataFrame,
    depot_id: str,
    db,
) -> dict:
    """Load RF model from S3 and score all batches."""
    store = ModelStore(db_session=db)
    model = store.load("expiry_risk")

    threshold_obj = store.load("expiry_risk_threshold")
    threshold     = threshold_obj.get("threshold", 0.5)

    # Fetch movement data needed for velocity features
    mov_rows = await db.execute("""
        SELECT batch_id::text, quantity, movement_type,
               created_at
        FROM stock_movements
        WHERE depot_id = :did
          AND created_at >= NOW() - INTERVAL '30 days'
    """, {"did": depot_id})
    movements_df = pd.DataFrame(mov_rows.fetchall(), columns=[
        "batch_id", "quantity", "movement_type", "created_at"
    ])

    X, _ = build_expiry_features(batches_df, movements_df, demand_df)
    probs = model.predict_proba(X)[:, 1]

    results = {}
    for i, row in batches_df.iterrows():
        if i >= len(probs):
            break
        bid      = row["batch_id"]
        risk     = float(probs[i])
        velocity = float(
            X.iloc[i]["sales_velocity_weekly"]
            if i < len(X) else 0.0
        )
        liq_date = compute_liquidation_date(
            remaining_stock=float(row["quantity_remaining"]),
            sales_velocity_weekly=velocity,
        )
        results[bid] = {
            "expiry_risk_score":            round(risk, 3),
            "recommended_liquidation_date": liq_date.isoformat(),
            "method":                       "model",
        }
    return results


def _run_formula(batches_df: pd.DataFrame) -> dict:
    """
    Deterministic formula fallback for cold start.
    Uses time pressure + slow mover factor.
    Runs until _should_use_model() returns True.

    Formula:
        time_pressure   = 1 - (days_left / shelf_life)
        slow_mover      = 1.0 (conservative — no velocity data yet)
        risk_score      = time_pressure * 0.6 + slow_mover * 0.4
    """
    results = {}
    today   = pd.Timestamp.today()

    for _, row in batches_df.iterrows():
        expiry      = pd.Timestamp(row["expiry_date"])
        days_left   = max((expiry - today).days, 0)
        shelf_life  = max(row.get("default_shelf_life_days", 365), 1)
        remaining   = max(float(row.get("quantity_remaining", 0)), 0)

        time_pressure = 1 - (days_left / shelf_life)
        time_pressure = max(0.0, min(time_pressure, 1.0))

        # Conservative: assume slow mover until velocity data exists
        slow_mover = 0.5

        risk = min(time_pressure * 0.6 + slow_mover * 0.4, 1.0)
        liq  = compute_liquidation_date(
            remaining_stock=remaining,
            sales_velocity_weekly=1.0,   # conservative estimate
        )
        results[row["batch_id"]] = {
            "expiry_risk_score":            round(risk, 3),
            "recommended_liquidation_date": liq.isoformat(),
            "method":                       "formula",
        }
    return results


async def _should_use_model(depot_id: str, db) -> bool:
    """
    Switch from formula to ML model automatically.
    Requirements: 180+ days of data AND 50+ expired-batch labels.
    """
    row = await db.execute("""
        SELECT
            COUNT(*) FILTER (
                WHERE expiry_date < NOW()
                  AND quantity_remaining > 0
            )                                    AS expired_count,
            EXTRACT(
                DAY FROM NOW() - MIN(created_at)
            )                                    AS data_days
        FROM batches
        WHERE depot_id = :did
    """, {"did": depot_id})
    r = row.fetchone()
    expired_count = int(r.expired_count or 0)
    data_days     = int(r.data_days     or 0)
    return expired_count >= 50 and data_days >= 180


async def _write_predictions(
    depot_id: str,
    run_date: str,
    results: dict,
    db,
) -> None:
    for batch_id, pred in results.items():
        await db.execute("""
            INSERT INTO expiry_predictions
                (depot_id, batch_id, run_date,
                 expiry_risk_score,
                 recommended_liquidation_date,
                 method)
            VALUES
                (:did, :bid, :rd, :score, :ldate, :method)
            ON CONFLICT (depot_id, batch_id, run_date)
            DO UPDATE SET
                expiry_risk_score            = EXCLUDED.expiry_risk_score,
                recommended_liquidation_date = EXCLUDED.recommended_liquidation_date,
                method                       = EXCLUDED.method
        """, {
            "did":    depot_id,
            "bid":    batch_id,
            "rd":     run_date,
            "score":  pred["expiry_risk_score"],
            "ldate":  pred["recommended_liquidation_date"],
            "method": pred["method"],
        })
    await db.commit()


def predict_expiry_risk(batch_df: pd.DataFrame) -> pd.DataFrame:
    """
    Offline expiry risk scoring from a batch DataFrame.
    Called by orchestrator Stage 2 via asyncio.gather — no DB required.

    Args:
        batch_df: DataFrame with batch_id, product_id, depot_id,
                  expiry_date, shelf_life_days, and optionally
                  the full expiry FEATURE_COLS

    Returns:
        DataFrame with columns:
            batch_id, product_id, expiry_risk_prob,
            recommended_liquidation_date, method
        One row per batch.
    """
    if batch_df.empty:
        return pd.DataFrame(columns=[
            "batch_id", "product_id", "expiry_risk_prob",
            "recommended_liquidation_date", "method",
        ])

    df = batch_df.copy()

    try:
        store     = ModelStore()
        model     = store.load("expiry_risk")
        thresh_obj = store.load("expiry_risk_threshold")
        thresh_obj.get("threshold", 0.5)  # loaded for future use; formula uses risk directly
        use_model  = True
    except Exception:
        logger.warning("predict_expiry_risk: no saved model — using formula fallback")
        model     = None
        use_model = False

    records = []

    if use_model:
        try:
            X, _ = build_expiry_features(df)
            probs = model.predict_proba(X)[:, 1]
            for i, row in df.iterrows():
                if i >= len(probs):
                    break
                risk     = float(probs[i])
                velocity = float(X.iloc[i].get("sales_velocity_weekly", 1.0)) if hasattr(X.iloc[i], "get") else 1.0
                liq_date = compute_liquidation_date(
                    remaining_stock=float(row.get("quantity_remaining", row.get("quantity", 0))),
                    sales_velocity_weekly=max(velocity, 0.01),
                )
                records.append({
                    "batch_id":                      str(row["batch_id"]),
                    "product_id":                    str(row["product_id"]),
                    "expiry_risk_prob":               round(risk, 4),
                    "recommended_liquidation_date":   liq_date.isoformat(),
                    "method":                        "model",
                })
        except Exception as exc:
            logger.warning(f"predict_expiry_risk: model inference failed ({exc}) — formula fallback")
            use_model = False

    if not use_model:
        today = pd.Timestamp.today()
        for _, row in df.iterrows():
            expiry     = pd.Timestamp(row["expiry_date"])
            days_left  = max((expiry - today).days, 0)
            shelf_life = max(float(row.get("shelf_life_days", row.get("default_shelf_life_days", 365))), 1)
            remaining  = max(float(row.get("quantity_remaining", row.get("quantity", 0))), 0)

            time_pressure = max(0.0, min(1 - (days_left / shelf_life), 1.0))
            risk          = min(time_pressure * 0.6 + 0.5 * 0.4, 1.0)
            liq_date      = compute_liquidation_date(
                remaining_stock=remaining,
                sales_velocity_weekly=1.0,
            )
            records.append({
                "batch_id":                     str(row["batch_id"]),
                "product_id":                   str(row["product_id"]),
                "expiry_risk_prob":              round(risk, 4),
                "recommended_liquidation_date":  liq_date.isoformat(),
                "method":                       "formula",
            })

    result = pd.DataFrame(records)
    logger.info(
        f"predict_expiry_risk: {len(result)} batches scored | "
        f"method={'model' if use_model else 'formula'}"
    )
    return result