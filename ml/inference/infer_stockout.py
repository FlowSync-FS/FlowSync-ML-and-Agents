"""
ml/inference/infer_stockout.py

StockoutCalculator — pure arithmetic, no model file.
Called by orchestrator.py at Stage 2b (parallel with infer_expiry).

Formula:
    days_until_stockout = current_stock / predicted_daily_rate
    will_stockout       = days_until_stockout < lead_time_days

The ML value comes from using the demand model's predicted_daily_rate
instead of last week's average — this accounts for seasonal spikes
and festival demand that a simple average misses.
"""

import logging
from datetime import date

import pandas as pd

from ml.shared.config_loader import get, _defaults

logger = logging.getLogger("flowsync.infer.stockout")


async def run(
    depot_id: str,
    demand_results: dict,
    db,
) -> dict:
    """
    Compute stockout risk for all products in one depot.

    Args:
        depot_id:       UUID string
        demand_results: output from infer_demand.run()
                        {product_id: {predicted_daily_rate, ...}}
        db:             async SQLAlchemy session

    Returns:
        {
            product_id: {
                current_stock:       float,
                days_until_stockout: float,
                lead_time_days:      float,
                will_stockout:       bool,
            }
        }

    Side effects:
        Writes rows to stockout_calculations table (upsert).
    """
    default_lead = get("default_lead_time_days", db, default=7)

    # Current stock per product (sum of all batches)
    stock_rows = await db.execute("""
        SELECT
            sm.product_id::text,
            SUM(
                CASE WHEN sm.movement_type = 'IN'     THEN sm.quantity
                     WHEN sm.movement_type = 'RETURN' THEN sm.quantity
                     ELSE 0
                END
            ) - SUM(
                CASE WHEN sm.movement_type = 'OUT'      THEN sm.quantity
                     WHEN sm.movement_type = 'WRITE_OFF' THEN sm.quantity
                     ELSE 0
                END
            )                           AS current_stock,
            COALESCE(p.lead_time_days, :default_lead) AS lead_time_days
        FROM stock_movements sm
        JOIN products p ON p.id = sm.product_id
        WHERE sm.depot_id = :did
        GROUP BY sm.product_id, p.lead_time_days
        HAVING SUM(
            CASE WHEN sm.movement_type IN ('IN','RETURN') THEN sm.quantity ELSE 0 END
        ) - SUM(
            CASE WHEN sm.movement_type IN ('OUT','WRITE_OFF') THEN sm.quantity ELSE 0 END
        ) >= 0
    """, {"did": depot_id, "default_lead": default_lead})

    results = {}

    for row in stock_rows.fetchall():
        pid        = str(row.product_id)
        stock      = max(float(row.current_stock or 0), 0.0)
        lead_time  = float(row.lead_time_days or default_lead)

        # Use demand model's rate if available
        # Clip at 0.01 to prevent division by zero for slow movers
        daily_rate = demand_results.get(pid, {}).get(
            "predicted_daily_rate", 0.0
        )
        daily_rate = max(daily_rate, 0.01)

        days_left     = stock / daily_rate
        will_stockout = days_left < lead_time

        results[pid] = {
            "current_stock":       round(stock, 2),
            "days_until_stockout": round(days_left, 1),
            "lead_time_days":      lead_time,
            "will_stockout":       will_stockout,
        }

    stockout_count = sum(1 for v in results.values() if v["will_stockout"])
    logger.info(
        f"[{depot_id}] Stockout calc: {len(results)} products | "
        f"{stockout_count} at risk"
    )

    await _write_calculations(depot_id, date.today().isoformat(), results, db)
    return results


async def _write_calculations(
    depot_id: str,
    run_date: str,
    results: dict,
    db,
) -> None:
    """
    Upsert stockout calculations.
    Table: stockout_calculations (depot_id, product_id, run_date)
    """
    for product_id, calc in results.items():
        await db.execute("""
            INSERT INTO stockout_calculations
                (depot_id, product_id, run_date,
                 current_stock, days_until_stockout,
                 lead_time_days, will_stockout)
            VALUES
                (:did, :pid, :rd,
                 :stock, :days, :lead, :will)
            ON CONFLICT (depot_id, product_id, run_date)
            DO UPDATE SET
                current_stock       = EXCLUDED.current_stock,
                days_until_stockout = EXCLUDED.days_until_stockout,
                lead_time_days      = EXCLUDED.lead_time_days,
                will_stockout       = EXCLUDED.will_stockout
        """, {
            "did":   depot_id,
            "pid":   product_id,
            "rd":    run_date,
            "stock": calc["current_stock"],
            "days":  calc["days_until_stockout"],
            "lead":  calc["lead_time_days"],
            "will":  calc["will_stockout"],
        })
    await db.commit()


def compute_stockout_risk(
    stock_df: pd.DataFrame,
    forecast_df: pd.DataFrame,
) -> pd.DataFrame:
    """
    Compute stockout risk from stock levels and demand forecast.
    Called by orchestrator Stage 2 via asyncio.gather — no DB required.

    Args:
        stock_df:    DataFrame with product_id, depot_id, units_available
        forecast_df: DataFrame from predict_demand() with product_id,
                     predicted_daily_rate (may be empty)

    Returns:
        DataFrame with columns:
            product_id, depot_id, current_stock,
            days_until_stockout, lead_time_days, will_stockout
        One row per product_id.
    """
    if stock_df.empty:
        return pd.DataFrame(columns=[
            "product_id", "depot_id", "current_stock",
            "days_until_stockout", "lead_time_days", "will_stockout",
        ])

    default_lead = _defaults().get("default_lead_time_days", 7)

    # Build lookup: product_id → predicted_daily_rate
    rate_map: dict = {}
    if forecast_df is not None and not forecast_df.empty and "predicted_daily_rate" in forecast_df.columns:
        for _, row in forecast_df.iterrows():
            rate_map[str(row["product_id"])] = float(row["predicted_daily_rate"])

    records = []
    for _, row in stock_df.iterrows():
        pid        = str(row["product_id"])
        depot_id   = str(row.get("depot_id", "unknown"))
        stock      = max(float(row.get("units_available", 0)), 0.0)
        lead_time  = float(row.get("lead_time_days", default_lead))

        daily_rate    = max(rate_map.get(pid, 0.01), 0.01)
        days_left     = stock / daily_rate
        will_stockout = days_left < lead_time

        records.append({
            "product_id":          pid,
            "depot_id":            depot_id,
            "current_stock":       round(stock, 2),
            "days_until_stockout": round(days_left, 1),
            "lead_time_days":      lead_time,
            "will_stockout":       will_stockout,
        })

    result        = pd.DataFrame(records)
    at_risk_count = int(result["will_stockout"].sum())
    logger.info(
        f"compute_stockout_risk: {len(result)} products | "
        f"{at_risk_count} at risk"
    )
    return result