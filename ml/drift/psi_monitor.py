"""
ml/drift/psi_monitor.py

Population Stability Index (PSI) monitor for detecting input feature drift.
PSI < 0.10 = stable, 0.10-0.20 = slight drift, > 0.20 = significant drift.
Significant drift triggers retraining via auto_trainer.py.
"""

import logging

import numpy as np
import pandas as pd

logger = logging.getLogger("flowsync.drift.psi")

PSI_STABLE = 0.10
PSI_ALERT = 0.20


def compute_psi(
    baseline: pd.Series,
    current: pd.Series,
    n_bins: int = 10,
) -> float:
    """
    Compute Population Stability Index between two distributions.

    PSI = sum((current_pct - baseline_pct) * ln(current_pct / baseline_pct))

    Args:
        baseline: Training distribution
        current:  Recent production distribution
        n_bins:   Number of equal-width histogram bins

    Returns:
        PSI score (float, lower is better)
    """
    epsilon = 1e-6
    lo = min(baseline.min(), current.min())
    hi = max(baseline.max(), current.max())
    bins = np.linspace(lo, hi, n_bins + 1)

    base_counts, _ = np.histogram(baseline.dropna(), bins=bins)
    curr_counts, _ = np.histogram(current.dropna(), bins=bins)

    base_pct = (base_counts / max(len(baseline), 1)).clip(epsilon)
    curr_pct = (curr_counts / max(len(current), 1)).clip(epsilon)

    psi = float(np.sum((curr_pct - base_pct) * np.log(curr_pct / base_pct)))
    return round(psi, 4)


def monitor_feature_drift(
    baseline_df: pd.DataFrame,
    current_df: pd.DataFrame,
    feature_cols: list,
) -> dict:
    """
    Compute PSI for every feature column and summarise drift status.

    Args:
        baseline_df:  Training-time feature distribution
        current_df:   Recent inference-time feature distribution
        feature_cols: Feature column names to check

    Returns:
        Dict with:
            features: {col → {psi, status}}
            needs_retraining: bool
            n_features_drifted: int
    """
    feature_results = {}
    needs_retraining = False

    for col in feature_cols:
        if col not in baseline_df.columns or col not in current_df.columns:
            logger.warning(f"PSI skip: '{col}' missing from one DataFrame")
            continue

        psi = compute_psi(baseline_df[col], current_df[col])

        if psi >= PSI_ALERT:
            status = "drift_alert"
            needs_retraining = True
        elif psi >= PSI_STABLE:
            status = "slight_drift"
        else:
            status = "stable"

        feature_results[col] = {"psi": psi, "status": status}
        logger.info(f"PSI {col}: {psi:.4f} [{status}]")

    n_drifted = sum(
        1 for v in feature_results.values()
        if v["status"] == "drift_alert"
    )
    summary = {
        "features": feature_results,
        "needs_retraining": needs_retraining,
        "n_features_drifted": n_drifted,
    }
    logger.info(
        f"Drift summary: {n_drifted}/{len(feature_results)} features drifted, "
        f"needs_retraining={needs_retraining}"
    )
    return summary
