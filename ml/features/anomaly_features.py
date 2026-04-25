"""
ml/features/anomaly_features.py

Feature engineering for the AnomalyEngine.
MVP: Z-score per product_category (works from day one, no training data).
Month 6: Isolation Forest on these same features — swap ml/inference/infer_anomaly.py only.

One feature set, two model implementations.
The upgrade never touches this file.
"""

import logging
from typing import Dict, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger("flowsync.features.anomaly")

FEATURE_COLS = [
    "quantity",
    "hour_of_day",
    "day_of_week",
    "batch_age_days",
    "movement_type_enc",
    "quantity_zscore",
    "velocity_deviation",
]

# Encoding map for movement types
MOVEMENT_TYPE_MAP = {
    "IN":       0,
    "OUT":      1,
    "RETURN":   2,
    "WRITE_OFF": 3,
    "TRANSFER": 4,
}


def build_anomaly_features(
    movements_df: pd.DataFrame,
    lookback_days: int = 30,
) -> Tuple[pd.DataFrame, Dict[str, dict]]:
    """
    Build feature matrix for anomaly detection.

    movements_df columns required:
        movement_id, batch_id, product_id, depot_id,
        product_category, quantity, movement_type,
        created_at, batch_created_at

    Returns:
        X         — feature DataFrame indexed by movement_id
        baselines — {category: {mean, std}} for use at inference time
                    Save these alongside the model in registry
    """
    df = movements_df.copy()
    df["created_at"]       = pd.to_datetime(df["created_at"])
    df["batch_created_at"] = pd.to_datetime(df["batch_created_at"])
    today = pd.Timestamp.today()

    # Temporal features
    df["hour_of_day"]    = df["created_at"].dt.hour
    df["day_of_week"]    = df["created_at"].dt.dayofweek
    df["batch_age_days"] = (
        (df["created_at"] - df["batch_created_at"])
        .dt.days.clip(lower=0)
    )

    # Categorical encoding
    df["movement_type_enc"] = (
        df["movement_type"]
        .map(MOVEMENT_TYPE_MAP)
        .fillna(0)
        .astype(int)
    )

    # Z-score per product_category
    # One baseline per category because antibiotics and OTC vitamins
    # have completely different movement distributions.
    # A global baseline generates 80% false positives on high-volume OTC.
    recent = df[
        df["created_at"] >= today - pd.Timedelta(days=lookback_days)
    ]
    baselines: Dict[str, dict] = {}

    for category, group in recent.groupby("product_category"):
        qty  = group["quantity"]
        mean = float(qty.mean())
        std  = float(qty.std())
        if std == 0 or np.isnan(std):
            std = 1.0  # prevent division by zero for single-SKU categories
        baselines[str(category)] = {"mean": mean, "std": std}

    def _zscore(row) -> float:
        cat = str(row["product_category"])
        if cat not in baselines:
            return 0.0
        b = baselines[cat]
        return float((row["quantity"] - b["mean"]) / b["std"])

    df["quantity_zscore"] = df.apply(_zscore, axis=1)

    # Velocity deviation
    # How much does today's quantity deviate from this product's own 30d avg?
    product_avg = (
        recent.groupby("product_id")["quantity"]
        .mean()
        .rename("product_avg")
    )
    df = df.join(product_avg, on="product_id")
    df["velocity_deviation"] = (
        (df["quantity"] - df["product_avg"].fillna(df["quantity"])) /
        df["product_avg"].fillna(1).clip(lower=1)
    )

    if "movement_id" in df.columns:
        X = df.set_index("movement_id")[FEATURE_COLS]
    else:
        X = df[FEATURE_COLS]

    logger.info(
        f"Anomaly features: {len(X)} movements | "
        f"{len(baselines)} category baselines built"
    )
    return X, baselines


def score_movement_zscore(
    quantity: float,
    product_category: str,
    baselines: Dict[str, dict],
) -> float:
    """
    Compute Z-score for a single stock movement at inference time.
    Called by infer_anomaly.py for each movement.

    Returns absolute Z-score (positive float).
    Higher = more anomalous.
    """
    if product_category not in baselines:
        return 0.0
    b = baselines[product_category]
    return abs((quantity - b["mean"]) / max(b["std"], 1.0))