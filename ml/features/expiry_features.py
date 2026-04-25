"""
ml/features/expiry_features.py

Feature engineering for ExpiryRiskModel (Slow Mover Forecaster).
Operates at BATCH level, not product level.

Four exact inputs from Problems.pdf spec:
    1. sales_velocity_weekly
    2. days_till_expiry
    3. seasonality_flag
    4. demand_trend_slope (from DemandForecaster output)

Imported by both training notebook and inference script.
Never duplicate this logic.
"""

import logging
from datetime import date

import numpy as np
import pandas as pd

logger = logging.getLogger("flowsync.features.expiry")

FEATURE_COLS = [
    "sales_velocity_weekly",
    "days_till_expiry",
    "pct_life_remaining",
    "seasonality_flag",
    "demand_trend_slope",
    "velocity_ratio",
    "category_risk_score",
    "is_cold_chain",
    "shelf_life_days",
]


def build_expiry_features(
    batches_df: pd.DataFrame,
    movements_df: pd.DataFrame,
    demand_preds_df: pd.DataFrame = None,
    category_avg_velocity: dict = None,
) -> tuple[pd.DataFrame, pd.Series]:
    """
    Build feature matrix for ExpiryRiskModel.

    batches_df columns required:
        batch_id, product_id, depot_id, expiry_date,
        quantity_received, quantity_remaining,
        product_category, is_cold_chain,
        default_shelf_life_days, created_at

    movements_df columns required:
        batch_id, quantity, movement_type, created_at

    demand_preds_df columns required (optional):
        product_id, depot_id, demand_trend_slope

    Returns:
        X — feature DataFrame (FEATURE_COLS)
        y — label Series (1=expired unsold, 0=sold before expiry)
            NaN for current batches (not yet expired)
    """
    df = batches_df.copy()
    df["expiry_date"] = pd.to_datetime(df["expiry_date"])
    df["created_at"]  = pd.to_datetime(df["created_at"])
    today = pd.Timestamp.today().normalize()

    # Feature 1: sales_velocity_weekly
    # Primary: units sold per week in the 30 days before today (or before expiry
    # for expired batches, so historical training data gets meaningful velocity).
    out_mvmt = movements_df[movements_df["movement_type"] == "OUT"].copy()
    out_mvmt["created_at"] = pd.to_datetime(out_mvmt["created_at"])

    recent_cutoff = today - pd.Timedelta(days=30)
    recent_sales = (
        out_mvmt[out_mvmt["created_at"] >= recent_cutoff]
        .groupby("batch_id")["quantity"]
        .sum()
        .div(30 / 7)
        .rename("sales_velocity_weekly")
        .reset_index()
    )
    df = df.merge(recent_sales, on="batch_id", how="left")
    df["sales_velocity_weekly"] = df["sales_velocity_weekly"].fillna(0.0)

    # Fallback for batches with no recent movements (e.g. historical training batches):
    # use lifetime average velocity so the feature is non-zero and meaningful.
    zero_velocity = df["sales_velocity_weekly"] == 0.0
    if zero_velocity.any():
        all_sales = (
            out_mvmt.groupby("batch_id")["quantity"]
            .sum()
            .rename("_total_sold")
            .reset_index()
        )
        df = df.merge(all_sales, on="batch_id", how="left")
        df["_total_sold"] = df["_total_sold"].fillna(0.0)
        effective_end = df["expiry_date"].clip(upper=today)
        batch_age_weeks = ((effective_end - df["created_at"]).dt.days / 7).clip(lower=1.0)
        lifetime_vel = df["_total_sold"] / batch_age_weeks
        df["sales_velocity_weekly"] = df["sales_velocity_weekly"].where(
            ~zero_velocity, lifetime_vel
        )
        df = df.drop(columns=["_total_sold"])

    # Feature 2: days_till_expiry
    df["days_till_expiry"] = (df["expiry_date"] - today).dt.days
    df["shelf_life_days"]  = df["default_shelf_life_days"].clip(lower=1)
    df["pct_life_remaining"] = (
        df["days_till_expiry"] / df["shelf_life_days"]
    ).clip(lower=0, upper=1)

    # Feature 3: seasonality_flag
    # 1 = product is in peak demand season right now
    df["seasonality_flag"] = df.apply(
        lambda r: _is_peak_season(r["product_category"], today.month),
        axis=1,
    ).astype(int)

    # Feature 4: demand_trend_slope (from DemandForecaster)
    # Negative slope near expiry = very high risk
    if demand_preds_df is not None and not demand_preds_df.empty:
        slope_map = demand_preds_df.set_index(
            ["product_id", "depot_id"]
        )["demand_trend_slope"].to_dict()
        df["demand_trend_slope"] = df.apply(
            lambda r: slope_map.get(
                (r["product_id"], r["depot_id"]), 0.0
            ),
            axis=1,
        )
    else:
        df["demand_trend_slope"] = 0.0

    # Derived: velocity_ratio
    # How does this batch compare to its category average?
    # < 1.0 = selling slower than peers → elevated risk
    if category_avg_velocity:
        df["avg_cat_velocity"] = (
            df["product_category"].map(category_avg_velocity).fillna(1.0)
        )
    else:
        df["avg_cat_velocity"] = (
            df.groupby("product_category")["sales_velocity_weekly"]
            .transform("mean")
            .fillna(1.0)
        )
    df["velocity_ratio"] = (
        df["sales_velocity_weekly"] /
        df["avg_cat_velocity"].clip(lower=0.1)
    ).clip(upper=5.0)

    # Derived: category_risk_score
    # Historical base rate of expiry by drug category
    CATEGORY_RISK = {
        "antibiotic":    0.25,
        "cold_chain":    0.40,
        "seasonal_otc":  0.35,
        "vitamin":       0.15,
        "pain_relief":   0.10,
        "antidiabetic":  0.20,
        "cardiac":       0.20,
        "gi":            0.18,
        "respiratory":   0.22,
        "thyroid":       0.12,
    }
    df["category_risk_score"] = (
        df["product_category"]
        .str.lower()
        .map(CATEGORY_RISK)
        .fillna(0.20)
    )
    df["is_cold_chain"] = df["is_cold_chain"].astype(int)

    # Target label (training only — historical batches)
    historical_mask = df["expiry_date"] < today
    df["label"] = np.where(
        historical_mask,
        (df["quantity_remaining"] > 0).astype(int),
        np.nan,
    )

    X = df[FEATURE_COLS].fillna(0)
    y = df["label"]

    logger.info(
        f"Expiry features: {len(X)} batches | "
        f"labelled: {historical_mask.sum()} | "
        f"current: {(~historical_mask).sum()}"
    )
    return X, y


def compute_liquidation_date(
    remaining_stock: float,
    sales_velocity_weekly: float,
    safety_buffer_days: int = 14,
) -> date:
    """
    Deterministic formula for recommended liquidation date.
    Problems.pdf spec: today + days_to_clear + safety_buffer

    Args:
        remaining_stock: units left in the batch
        sales_velocity_weekly: units sold per week currently
        safety_buffer_days: logistics + retailer acceptance buffer
    """
    if sales_velocity_weekly <= 0:
        # Not selling at all — liquidate urgently
        return (
            pd.Timestamp.today() + pd.Timedelta(days=7)
        ).date()

    daily_rate    = sales_velocity_weekly / 7
    days_to_clear = remaining_stock / daily_rate
    return (
        pd.Timestamp.today() +
        pd.Timedelta(days=int(days_to_clear) + safety_buffer_days)
    ).date()


def _is_peak_season(category: str, month: int) -> int:
    """
    Heuristic peak season map for Indian pharma depot context.
    Returns 1 if current month is in peak demand season.
    """
    PEAK_MONTHS = {
        "antibiotic":    [10, 11, 12, 1, 2, 3],
        "cold_chain":    list(range(1, 13)),
        "otc_cold":      [10, 11, 12, 1, 2],
        "ors":           [6, 7, 8, 9],
        "antidiabetic":  list(range(1, 13)),
        "vitamin":       [10, 11, 12, 1],
        "pain_relief":   list(range(1, 13)),
        "gi":            [4, 5, 6, 7, 8, 9],
        "respiratory":   [10, 11, 12, 1, 2],
        "cardiac":       list(range(1, 13)),
        "thyroid":       list(range(1, 13)),
    }
    cat   = (category or "").lower().replace(" ", "_")
    peaks = PEAK_MONTHS.get(cat, list(range(1, 13)))
    return int(month in peaks)