"""
ml/inference/infer_demand.py

DemandForecaster production inference.
Called by orchestrator.py at Stage 1 — runs first, everything depends on it.

Loads per-depot model if available, else global.
Writes results to demand_predictions table.
Returns dict for downstream stages to consume.
"""

import logging
from datetime import date

import numpy as np
import pandas as pd

from ml.features.demand_features import (
    build_demand_features,
    build_inference_features,
    fill_missing_dates,
    FEATURE_COLS,
)
from ml.registry.model_store import ModelStore
from ml.shared.config_loader import get, _defaults

logger = logging.getLogger("flowsync.infer.demand")


async def run(
    depot_id: str,
    run_date: str,
    db,
) -> dict:
    """
    Run demand forecasting for one depot.

    Args:
        depot_id: UUID string of the depot
        run_date: ISO date string e.g. '2025-10-20'
        db:       async SQLAlchemy session

    Returns:
        {
            product_id: {
                predicted_units_14d:  float,
                predicted_daily_rate: float,
                demand_trend_slope:   float,
            }
        }

    Side effects:
        Writes rows to demand_predictions table (upsert).
    """
    festival_dates = get("festival_dates", db, default=[])
    store          = ModelStore(db_session=db)

    # Load per-depot model if it exists, fall back to global
    try:
        model = store.load(f"demand_{depot_id}", fallback="demand_global")
        logger.info(f"[{depot_id}] Demand: loaded per-depot model")
    except FileNotFoundError:
        logger.warning(
            f"[{depot_id}] No demand model found — "
            "ensure demand_global is trained and saved first"
        )
        return {}

    # Pull last 60 days of OUT movements for this depot
    rows = await db.execute("""
        SELECT
            sm.product_id::text,
            sm.depot_id::text,
            DATE(sm.created_at)            AS date,
            SUM(sm.quantity)               AS units_sold,
            p.product_category,
            p.is_cold_chain,
            d.region                       AS depot_region
        FROM stock_movements sm
        JOIN products p ON p.id = sm.product_id
        JOIN depots   d ON d.id = sm.depot_id
        WHERE sm.depot_id   = :did
          AND sm.movement_type = 'OUT'
          AND sm.created_at >= NOW() - INTERVAL '60 days'
        GROUP BY
            sm.product_id, sm.depot_id, DATE(sm.created_at),
            p.product_category, p.is_cold_chain, d.region
    """, {"did": depot_id})

    df = pd.DataFrame(rows.fetchall(), columns=[
        "product_id", "depot_id", "date", "units_sold",
        "product_category", "is_cold_chain", "depot_region",
    ])

    if df.empty:
        logger.warning(
            f"[{depot_id}] No sales data in last 60 days — "
            "skipping demand inference"
        )
        return {}

    # Build features using the shared features file
    # (same function used during training — no duplication)
    X, feature_rows = build_inference_features(
        df,
        festival_dates=festival_dates,
        return_frame=True,
    )

    if X.empty:
        logger.warning(f"[{depot_id}] Feature matrix empty after build")
        return {}

    preds = model.predict(X).clip(min=0)

    feature_rows = feature_rows.copy()
    feature_rows["predicted_daily_rate"] = preds

    # Build results dict keyed by product_id using all feature rows for that product.
    # A product can appear on multiple dates; aggregate the per-row model output.
    results = {}
    for pid, grp in feature_rows.groupby("product_id", sort=False):
        product_df  = df[df["product_id"] == pid].sort_values("date")
        daily_rate  = float(grp["predicted_daily_rate"].mean())
        units_14d   = daily_rate * 14
        trend_slope = _compute_trend_slope(product_df)

        results[str(pid)] = {
            "predicted_units_14d":  round(units_14d,   2),
            "predicted_daily_rate": round(daily_rate,   4),
            "demand_trend_slope":   round(trend_slope,  4),
        }

    await _write_predictions(depot_id, run_date, results, db)

    logger.info(
        f"[{depot_id}] Demand predictions written: "
        f"{len(results)} products"
    )
    return results


def _compute_trend_slope(product_df: pd.DataFrame) -> float:
    """
    Linear regression slope on last 30 days of daily sales.
    Positive = growing demand, negative = falling demand.
    Used as demand_trend_slope feature in expiry risk model.
    """
    if len(product_df) < 5:
        return 0.0
    x     = np.arange(len(product_df), dtype=float)
    y     = product_df["units_sold"].values.astype(float)
    slope = float(np.polyfit(x, y, 1)[0])
    return slope


async def _write_predictions(
    depot_id: str,
    run_date: str,
    results: dict,
    db,
) -> None:
    """
    Upsert demand predictions for the run date.
    ON CONFLICT updates existing row — safe to re-run.
    """
    for product_id, pred in results.items():
        await db.execute("""
            INSERT INTO demand_predictions
                (depot_id, product_id, run_date,
                 predicted_units_14d, predicted_daily_rate,
                 demand_trend_slope)
            VALUES
                (:did, :pid, :rd, :u14, :rate, :slope)
            ON CONFLICT (depot_id, product_id, run_date)
            DO UPDATE SET
                predicted_units_14d  = EXCLUDED.predicted_units_14d,
                predicted_daily_rate = EXCLUDED.predicted_daily_rate,
                demand_trend_slope   = EXCLUDED.demand_trend_slope
        """, {
            "did":   depot_id,
            "pid":   product_id,
            "rd":    run_date,
            "u14":   pred["predicted_units_14d"],
            "rate":  pred["predicted_daily_rate"],
            "slope": pred["demand_trend_slope"],
        })
    await db.commit()


def predict_demand(sales_df: pd.DataFrame) -> pd.DataFrame:
    """
    Offline demand prediction from a sales DataFrame.
    Called by orchestrator Stage 1 via asyncio.to_thread — no DB required.

    Args:
        sales_df: DataFrame with product_id, depot_id, date, units_sold,
                  product_category, is_cold_chain, depot_region

    Returns:
        DataFrame with columns:
            product_id, depot_id, predicted_units_14d,
            predicted_daily_rate, demand_trend_slope
        One row per product_id.
    """
    if sales_df.empty:
        return pd.DataFrame(columns=[
            "product_id", "depot_id",
            "predicted_units_14d", "predicted_daily_rate", "demand_trend_slope",
        ])

    festival_dates = _defaults().get("festival_dates", [])

    try:
        store = ModelStore()
        model = store.load("demand_global")
    except Exception:
        logger.warning("predict_demand: no saved model — using last-30d average fallback")
        model = None

    df = sales_df.copy()
    df["date"] = pd.to_datetime(df["date"])
    df = fill_missing_dates(df)
    X, _, feature_rows = build_demand_features(
        df,
        festival_dates=festival_dates,
        return_frame=True,
    )

    if model is not None:
        preds = model.predict(X).clip(min=0)
        feature_rows = feature_rows.copy()
        feature_rows["predicted_daily_rate"] = preds

    records = []
    depot_id = str(df["depot_id"].iloc[0]) if "depot_id" in df.columns else "unknown"

    for pid, product_df in df.groupby("product_id", sort=False):
        product_df = product_df.sort_values("date")

        if model is not None:
            try:
                pid_rates = feature_rows.loc[
                    feature_rows["product_id"].astype(str) == str(pid),
                    "predicted_daily_rate",
                ]
                if not pid_rates.empty:
                    daily_rate = float(pid_rates.mean())
                else:
                    daily_rate = float(product_df["units_sold"].mean())
            except Exception:
                daily_rate = float(product_df["units_sold"].mean())
        else:
            daily_rate = float(product_df["units_sold"].tail(30).mean())

        daily_rate  = max(daily_rate, 0.0)
        trend_slope = _compute_trend_slope(product_df)

        records.append({
            "product_id":            str(pid),
            "depot_id":              depot_id,
            "predicted_units_14d":   round(daily_rate * 14, 2),
            "predicted_daily_rate":  round(daily_rate, 4),
            "demand_trend_slope":    round(trend_slope, 4),
        })

    result = pd.DataFrame(records)
    logger.info(
        f"predict_demand: {len(result)} products | "
        f"model={'loaded' if model else 'fallback'}"
    )
    return result