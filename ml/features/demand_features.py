"""
ml/features/demand_features.py

Shared feature engineering for DemandForecaster.
Imported by BOTH ml/models/demand_forecaster.ipynb (training)
AND ml/inference/infer_demand.py (production).

CRITICAL: Never duplicate this logic in either place.
Training-serving skew = wrong predictions silently in production.
"""

import logging

import numpy as np
import pandas as pd

logger = logging.getLogger("flowsync.features.demand")

# Exact feature columns used by XGBoost — order matters
FEATURE_COLS = [
    "lag_7",
    "lag_14",
    "lag_30",
    "rolling_avg_7",
    "rolling_avg_30",
    "rolling_std_7",
    "month_sin",
    "month_cos",
    "day_of_week_sin",
    "day_of_week_cos",
    "is_festival_week",
    "season_code",
    "product_category_enc",
    "depot_region_enc",
    "is_cold_chain",
]

TARGET_COL = "units_sold"


def build_demand_features(
    df: pd.DataFrame,
    festival_dates: list = None,
) -> tuple[pd.DataFrame, pd.Series]:
    """
    Build feature matrix for XGBoost demand forecaster.

    Input df must have columns:
        product_id, depot_id, date, units_sold,
        product_category, depot_region, is_cold_chain

    Returns:
        X   — feature DataFrame (FEATURE_COLS)
        y   — target Series (units_sold)
    """
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values(["product_id", "depot_id", "date"])

    festival_dates = festival_dates or []
    festival_set = set(
        pd.to_datetime(festival_dates).strftime("%Y-%m-%d")
    )

    grp = df.groupby(["product_id", "depot_id"])["units_sold"]

    # Lag features — captures weekly ordering patterns
    # and monthly cycles (salary paydays affect retailer ordering)
    df["lag_7"]  = grp.shift(7)
    df["lag_14"] = grp.shift(14)
    df["lag_30"] = grp.shift(30)

    # Rolling statistics
    # shift(1) prevents data leakage: today's sales
    # not used to predict today
    df["rolling_avg_7"] = grp.transform(
        lambda x: x.shift(1).rolling(7, min_periods=3).mean()
    )
    df["rolling_avg_30"] = grp.transform(
        lambda x: x.shift(1).rolling(30, min_periods=10).mean()
    )
    df["rolling_std_7"] = grp.transform(
        lambda x: x.shift(1).rolling(7, min_periods=3).std().fillna(0)
    )

    # Cyclical calendar encoding
    # sin/cos so December → January is continuous, not a jump
    df["month_sin"] = np.sin(2 * np.pi * df["date"].dt.month / 12)
    df["month_cos"] = np.cos(2 * np.pi * df["date"].dt.month / 12)
    df["day_of_week_sin"] = np.sin(
        2 * np.pi * df["date"].dt.dayofweek / 7
    )
    df["day_of_week_cos"] = np.cos(
        2 * np.pi * df["date"].dt.dayofweek / 7
    )

    # Festival flag: within ±7 days of any major Indian festival
    # Pharma demand spikes 2-3x in festival weeks (OTC, vitamins)
    df["is_festival_week"] = df["date"].apply(
        lambda d: int(
            any(
                abs((d - pd.Timestamp(f)).days) <= 7
                for f in festival_set
            )
        )
    )

    # Indian season code
    # 0=winter(Nov-Feb), 1=summer(Mar-May),
    # 2=monsoon(Jun-Sep), 3=post-monsoon(Oct)
    def _season(month: int) -> int:
        if month in [11, 12, 1, 2]:
            return 0
        if month in [3, 4, 5]:
            return 1
        if month in [6, 7, 8, 9]:
            return 2
        return 3

    df["season_code"] = df["date"].dt.month.apply(_season)

    # Categorical encoding (label encoding — XGBoost handles natively)
    df["product_category_enc"] = (
        df["product_category"].astype("category").cat.codes
    )
    df["depot_region_enc"] = (
        df["depot_region"].astype("category").cat.codes
    )
    df["is_cold_chain"] = df["is_cold_chain"].astype(int)

    # Drop rows with NaN lags (first 30 days per product+depot)
    df_clean = df.dropna(
        subset=["lag_7", "lag_14", "lag_30",
                "rolling_avg_7", "rolling_avg_30"]
    )

    if df_clean.empty:
        raise ValueError(
            "No rows after dropping lag NaNs. "
            "Depot needs at least 30 days of sales history."
        )

    logger.info(
        f"Demand features: {len(df_clean)} rows "
        f"({len(df) - len(df_clean)} dropped for lag warmup)"
    )

    X = df_clean[FEATURE_COLS].reset_index(drop=True)
    y = df_clean[TARGET_COL].reset_index(drop=True)
    return X, y


def build_inference_features(
    df: pd.DataFrame,
    festival_dates: list = None,
) -> pd.DataFrame:
    """
    Same as build_demand_features but returns only X (no y).
    Used at inference time when ground truth doesn't exist.
    Fills remaining NaNs with column medians for graceful degradation
    on products with < 30 days history.
    """
    X, _ = build_demand_features(df, festival_dates)
    for col in FEATURE_COLS:
        if X[col].isna().any():
            X[col] = X[col].fillna(X[col].median())
    return X


def fill_missing_dates(df: pd.DataFrame) -> pd.DataFrame:
    """
    Fill gaps in time series with zeros.
    Depots don't sell every product every day — missing dates
    must be zero, not absent, or lag features compute incorrectly.
    """
    df["date"] = pd.to_datetime(df["date"])
    date_range = pd.date_range(df["date"].min(), df["date"].max())

    # Preserve existing product/depot pairings rather than building a full
    # product x depot cartesian product. Rossmann-style depot data is sparse,
    # so the full cross join would invent impossible combinations.
    pair_rows = df[["product_id", "depot_id"]].drop_duplicates()
    full_idx = pd.MultiIndex.from_tuples(
        [
            (row.product_id, row.depot_id, current_date)
            for row in pair_rows.itertuples(index=False)
            for current_date in date_range
        ],
        names=["product_id", "depot_id", "date"],
    )

    df_full = (
        df.set_index(["product_id", "depot_id", "date"])
        .reindex(full_idx)
        .reset_index()
    )

    metadata_cols = [
        col for col in ["product_category", "depot_region", "is_cold_chain"]
        if col in df.columns
    ]

    if metadata_cols:
        pair_meta = (
            df.drop_duplicates(["product_id", "depot_id"])
            [["product_id", "depot_id", *metadata_cols]]
        )
        df_full = df_full.drop(columns=metadata_cols, errors="ignore")
        df_full = df_full.merge(
            pair_meta,
            on=["product_id", "depot_id"],
            how="left",
        )

    numeric_cols = [
        col for col in df_full.columns
        if col not in {"product_id", "depot_id", "date", *metadata_cols}
    ]
    for col in numeric_cols:
        df_full[col] = df_full[col].fillna(0)

    return df_full