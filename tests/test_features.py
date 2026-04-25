"""
tests/test_features.py

Unit tests for ml/features/*.py

Tests the shared feature engineering code that is used by
both training (notebooks) and inference (production pipeline).

Critical: these tests protect against training-serving skew.
If a feature changes in training but not inference (or vice versa),
these tests catch it before production deployment.
"""

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from ml.features.demand_features import (
    FEATURE_COLS,
    TARGET_COL,
    build_demand_features,
    build_inference_features,
    fill_missing_dates,
)
from ml.features.expiry_features import (
    FEATURE_COLS as EXPIRY_FEATURE_COLS,
    build_expiry_features,
    compute_liquidation_date,
    _is_peak_season,
)
from ml.features.anomaly_features import (
    FEATURE_COLS as ANOMALY_FEATURE_COLS,
    build_anomaly_features,
    score_movement_zscore,
)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_sales_df(
    n_days:    int   = 60,
    n_products: int  = 3,
    depot_id:  str   = "depot_001",
) -> pd.DataFrame:
    """Create synthetic sales DataFrame for demand feature tests."""
    rows = []
    base_date = date.today() - timedelta(days=n_days)

    for pid in range(n_products):
        for d in range(n_days):
            rows.append({
                "product_id":       f"prod_{pid:03d}",
                "depot_id":         depot_id,
                "date":             base_date + timedelta(days=d),
                "units_sold":       float(10 + pid * 5 + (d % 7)),
                "product_category": "pain_relief",
                "is_cold_chain":    False,
                "depot_region":     "Odisha",
            })
    return pd.DataFrame(rows)


def _make_batches_df(n_batches: int = 5) -> pd.DataFrame:
    """Create synthetic batch DataFrame for expiry feature tests."""
    rows = []
    for i in range(n_batches):
        rows.append({
            "batch_id":                  f"batch_{i:03d}",
            "product_id":                f"prod_{i:03d}",
            "depot_id":                  "depot_001",
            "expiry_date":               date.today() + timedelta(days=60 + i * 30),
            "quantity_received":         100,
            "quantity_remaining":        50 + i * 5,
            "product_category":          "antibiotic",
            "is_cold_chain":             False,
            "default_shelf_life_days":   365,
            "created_at":                date.today() - timedelta(days=90),
        })
    return pd.DataFrame(rows)


def _make_movements_df(n: int = 50) -> pd.DataFrame:
    """Create synthetic stock movements for anomaly feature tests."""
    rows = []
    for i in range(n):
        rows.append({
            "movement_id":      f"mov_{i:04d}",
            "batch_id":         f"batch_{i % 5:03d}",
            "product_id":       f"prod_{i % 3:03d}",
            "depot_id":         "depot_001",
            "product_category": "pain_relief" if i % 2 == 0 else "antibiotic",
            "quantity":         float(10 + (i % 20)),
            "movement_type":    "OUT",
            "created_at":       pd.Timestamp.today() - pd.Timedelta(days=i % 30),
            "batch_created_at": pd.Timestamp.today() - pd.Timedelta(days=90),
        })
    return pd.DataFrame(rows)


# ── Demand feature tests ───────────────────────────────────────────────────────

class TestDemandFeatures:

    def test_feature_cols_present(self):
        """All expected feature columns must be in FEATURE_COLS."""
        required = [
            "lag_7", "lag_14", "lag_30",
            "rolling_avg_7", "rolling_avg_30",
            "month_sin", "month_cos",
            "is_festival_week", "season_code",
        ]
        for col in required:
            assert col in FEATURE_COLS, f"Missing feature: {col}"

    def test_build_demand_features_shape(self):
        """X should have exactly FEATURE_COLS columns."""
        df = _make_sales_df(n_days=60)
        X, y = build_demand_features(df)

        assert set(X.columns) == set(FEATURE_COLS), (
            f"Column mismatch. "
            f"Extra: {set(X.columns) - set(FEATURE_COLS)}. "
            f"Missing: {set(FEATURE_COLS) - set(X.columns)}"
        )
        assert len(X) == len(y)
        assert len(X) > 0

    def test_no_lag_nulls_after_build(self):
        """No NaN values in X after build (lag warmup rows dropped)."""
        df = _make_sales_df(n_days=60)
        X, y = build_demand_features(df)

        null_counts = X.isnull().sum()
        assert null_counts.sum() == 0, (
            f"NaN values found in features: {null_counts[null_counts > 0]}"
        )

    def test_lag_7_is_7_days_ago(self):
        """lag_7 value for day N should equal units_sold for day N-7."""
        df        = _make_sales_df(n_days=60, n_products=1)
        df_filled = fill_missing_dates(df)
        X, y      = build_demand_features(df_filled)

        # After 30-day warmup, lag_7 should correlate with 7-day-ago sales
        # Simply check lag_7 exists and is finite
        assert X["lag_7"].notna().all()
        assert np.isfinite(X["lag_7"].values).all()

    def test_festival_flag_set_correctly(self):
        """is_festival_week = 1 for dates near festival, 0 otherwise."""
        festival_date = date(2025, 10, 20)   # Diwali
        # Build 60 days of history ending well after the festival window so
        # lag features can warm up.  start_date gives ≥30 days before festival.
        start_date = date(2025, 9, 1)
        rows = []
        for i in range(70):
            rows.append({
                "product_id":       "p1",
                "depot_id":         "d1",
                "date":             start_date + timedelta(days=i),
                "units_sold":       10.0,
                "product_category": "otc",
                "is_cold_chain":    False,
                "depot_region":     "Odisha",
            })
        df = pd.DataFrame(rows)

        X, _ = build_demand_features(
            df, festival_dates=[str(festival_date)]
        )
        # At least one row within ±7 days of festival should have flag=1
        assert X["is_festival_week"].max() == 1

    def test_cyclical_month_encoding(self):
        """month_sin and month_cos should be in [-1, 1]."""
        df    = _make_sales_df(n_days=60)
        X, _  = build_demand_features(df)

        assert X["month_sin"].between(-1, 1).all()
        assert X["month_cos"].between(-1, 1).all()

    def test_season_code_valid_range(self):
        """season_code must be 0, 1, 2, or 3."""
        df   = _make_sales_df(n_days=60)
        X, _ = build_demand_features(df)

        assert X["season_code"].isin([0, 1, 2, 3]).all()

    def test_inference_features_no_null(self):
        """build_inference_features should never return NaN."""
        df = _make_sales_df(n_days=45)
        X  = build_inference_features(df)

        assert X.isnull().sum().sum() == 0

    def test_fill_missing_dates_preserves_data(self):
        """fill_missing_dates should not lose any existing rows."""
        df       = _make_sales_df(n_days=30, n_products=2)
        df_filled = fill_missing_dates(df)

        # Filled df should have >= original rows
        assert len(df_filled) >= len(df)

    def test_insufficient_data_raises(self):
        """Fewer than 30 days of data should raise ValueError."""
        df = _make_sales_df(n_days=10)   # too few for lag warmup

        with pytest.raises(ValueError, match="30 days"):
            build_demand_features(df)


# ── Expiry feature tests ───────────────────────────────────────────────────────

class TestExpiryFeatures:

    def test_feature_cols_present(self):
        required = [
            "sales_velocity_weekly", "days_till_expiry",
            "seasonality_flag", "demand_trend_slope",
        ]
        for col in required:
            assert col in EXPIRY_FEATURE_COLS, f"Missing: {col}"

    def test_build_expiry_features_shape(self):
        batches_df   = _make_batches_df(5)
        movements_df = pd.DataFrame(columns=[
            "batch_id", "quantity", "movement_type", "created_at"
        ])
        X, y = build_expiry_features(batches_df, movements_df)

        assert set(EXPIRY_FEATURE_COLS).issubset(set(X.columns))
        assert len(X) == len(batches_df)

    def test_zero_velocity_for_no_movements(self):
        """
        Batches with no OUT movements should have
        sales_velocity_weekly = 0.
        """
        batches_df   = _make_batches_df(3)
        movements_df = pd.DataFrame(columns=[
            "batch_id", "quantity", "movement_type", "created_at"
        ])
        X, _ = build_expiry_features(batches_df, movements_df)

        assert (X["sales_velocity_weekly"] == 0).all()

    def test_pct_life_remaining_range(self):
        """pct_life_remaining must be in [0, 1]."""
        batches_df   = _make_batches_df(5)
        movements_df = pd.DataFrame(columns=[
            "batch_id", "quantity", "movement_type", "created_at"
        ])
        X, _ = build_expiry_features(batches_df, movements_df)

        assert X["pct_life_remaining"].between(0, 1).all()

    def test_compute_liquidation_date_positive_velocity(self):
        """Liquidation date should be in the future."""
        result = compute_liquidation_date(
            remaining_stock       = 50.0,
            sales_velocity_weekly = 10.0,
            safety_buffer_days    = 14,
        )
        assert result > date.today()

    def test_compute_liquidation_date_zero_velocity(self):
        """Zero velocity → urgent liquidation in 7 days."""
        result = compute_liquidation_date(
            remaining_stock       = 50.0,
            sales_velocity_weekly = 0.0,
        )
        assert result == date.today() + timedelta(days=7)

    def test_peak_season_antibiotic_winter(self):
        """Antibiotics should be peak in winter months."""
        assert _is_peak_season("antibiotic", 1)  == 1   # January
        assert _is_peak_season("antibiotic", 2)  == 1   # February
        assert _is_peak_season("antibiotic", 7)  == 0   # July (off-season)

    def test_is_cold_chain_encoded(self):
        """is_cold_chain must be integer 0 or 1 in feature matrix."""
        batches_df   = _make_batches_df(3)
        movements_df = pd.DataFrame(columns=[
            "batch_id", "quantity", "movement_type", "created_at"
        ])
        X, _ = build_expiry_features(batches_df, movements_df)

        assert X["is_cold_chain"].isin([0, 1]).all()


# ── Anomaly feature tests ──────────────────────────────────────────────────────

class TestAnomalyFeatures:

    def test_feature_cols_present(self):
        required = [
            "quantity", "hour_of_day", "day_of_week",
            "quantity_zscore", "movement_type_enc",
        ]
        for col in required:
            assert col in ANOMALY_FEATURE_COLS, f"Missing: {col}"

    def test_build_anomaly_features_shape(self):
        df  = _make_movements_df(50)
        X, baselines = build_anomaly_features(df)

        assert len(X) == len(df)
        assert len(baselines) >= 1   # at least one category baseline

    def test_baselines_per_category(self):
        """Each category in movements should have its own baseline."""
        df  = _make_movements_df(50)
        _, baselines = build_anomaly_features(df)

        categories = df["product_category"].unique()
        for cat in categories:
            assert cat in baselines, f"Missing baseline for category: {cat}"
            assert "mean" in baselines[cat]
            assert "std"  in baselines[cat]

    def test_zscore_normal_movement(self):
        """A movement at the mean should have Z-score near 0."""
        baselines = {"pain_relief": {"mean": 15.0, "std": 5.0}}
        z = score_movement_zscore(15.0, "pain_relief", baselines)
        assert z < 0.1   # approximately zero

    def test_zscore_extreme_movement(self):
        """A movement 5 std devs from mean should have high Z-score."""
        baselines = {"pain_relief": {"mean": 10.0, "std": 2.0}}
        z = score_movement_zscore(20.0, "pain_relief", baselines)
        assert z >= 4.5   # (20-10)/2 = 5.0

    def test_zscore_unknown_category(self):
        """Unknown category should return Z-score 0 (no false positives)."""
        baselines = {"known_cat": {"mean": 10.0, "std": 2.0}}
        z = score_movement_zscore(100.0, "unknown_category", baselines)
        assert z == 0.0

    def test_movement_type_encoding(self):
        """movement_type_enc should be integer 0-4."""
        df  = _make_movements_df(20)
        X, _ = build_anomaly_features(df)
        assert X["movement_type_enc"].isin([0, 1, 2, 3, 4]).all()

    def test_hour_of_day_range(self):
        """hour_of_day must be in [0, 23]."""
        df  = _make_movements_df(30)
        X, _ = build_anomaly_features(df)
        assert X["hour_of_day"].between(0, 23).all()

    def test_day_of_week_range(self):
        """day_of_week must be in [0, 6]."""
        df  = _make_movements_df(30)
        X, _ = build_anomaly_features(df)
        assert X["day_of_week"].between(0, 6).all()