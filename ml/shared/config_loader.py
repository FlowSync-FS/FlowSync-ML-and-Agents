"""
ml/shared/config_loader.py

Single source of truth for every threshold and rule.
ALL agents and inference files import from here.
Never hardcode thresholds anywhere else.
"""

import json
import logging
from typing import Any

logger = logging.getLogger("flowsync.config")

# Module-level cache — populated once per pipeline run by load_config().
# Empty dict means "not yet loaded — fall back to _defaults()".
_config_cache: dict = {}


async def load_config(db) -> None:
    """
    Async loader called once at the start of each pipeline run by orchestrator.
    Populates _config_cache from compliance_config table.
    Replaces the old synchronous get_config(db_session) which silently failed
    when called with an AsyncSession (sync .execute() on async session raises
    AttributeError, causing the cache to always return _defaults()).

    Args:
        db: async SQLAlchemy session (admin connection)

    Side effects:
        Overwrites module-level _config_cache.
    """
    global _config_cache
    try:
        rows = await db.execute("SELECT key, value FROM compliance_config")
        config: dict = {}
        for row in rows.fetchall():
            try:
                config[row.key] = json.loads(row.value)
            except (json.JSONDecodeError, TypeError):
                config[row.key] = row.value
        _config_cache = config
        logger.info(f"Loaded {len(_config_cache)} config keys from compliance_config")
    except Exception as e:
        logger.error(f"Config load failed: {e} — using hardcoded defaults")
        _config_cache = {}


def get_config() -> dict:
    """
    Return the current runtime config, falling back to hardcoded defaults.
    Call load_config(db) at pipeline start to populate from DB.
    """
    return _config_cache if _config_cache else _defaults()


def get(key: str, db_session=None, default: Any = None) -> Any:
    """
    Convenience accessor for a single config key.

    Args:
        key:        Config key to look up (e.g. 'expiry_critical_threshold').
        db_session: Ignored — kept for call-site backward compatibility.
                    Config is loaded once at pipeline start via load_config(db).
        default:    Value to return if key is absent from both cache and defaults.

    Returns:
        Config value, or default if not found.
    """
    return get_config().get(key, _defaults().get(key, default))


def _defaults() -> dict:
    """
    Emergency fallback if DB is unreachable at 2 AM pipeline start.
    These values must exactly mirror seed_compliance_config.py.
    """
    return {
        # ML thresholds
        "fefo_override_threshold":       0.6,
        "expiry_critical_threshold":     0.85,
        "expiry_warning_threshold":      0.60,
        "anomaly_hold_threshold":        2.5,
        "anomaly_alert_threshold":       2.0,

        # Cold chain (°C)
        "temp_cold_chain_min":           2.0,
        "temp_cold_chain_max":           8.0,
        "temp_general_min":              15.0,
        "temp_general_max":              25.0,
        "temp_deep_freeze":              -20.0,

        # Alert windows (days before expiry)
        "expiry_alert_days_critical":    30,
        "expiry_alert_days_warning":     60,
        "expiry_alert_days_early":       120,

        # Business rules
        "default_lead_time_days":        7,
        "dso_warning_threshold":         60,
        "cashflow_reorder_reduction":    0.3,
        "exact_match_tolerance":         0.01,
        "consolidated_match_tolerance":  0.02,
        "consolidation_invoice_limit":   5,
        "approval_expiry_hours":         24,
        "iot_silence_alert_minutes":     35,

        # Indian festival dates — update annually
        "festival_dates": [
            "2025-10-02", "2025-10-12", "2025-10-20",
            "2025-10-23", "2025-11-05", "2025-11-15",
            "2025-12-25", "2026-01-14", "2026-01-26",
            "2026-03-14", "2026-04-06", "2026-04-14",
            "2026-06-13", "2026-08-15", "2026-09-29",
            "2026-10-20", "2026-11-08",
        ],

        # ML model training targets — used by auto_trainer.py and notebooks
        "mape_target":   0.15,
        "auc_target":    0.80,
        "recall_target": 0.85,

        # XGBoost hyperparameters (DemandForecaster)
        "xgboost": {
            "n_estimators":     500,
            "max_depth":        6,
            "learning_rate":    0.05,
            "subsample":        0.8,
            "colsample_bytree": 0.8,
            "min_child_weight": 3,
            "objective":        "reg:squarederror",
            "nthread":          -1,
            "tree_method":      "hist",
            "random_state":     42,
            "verbosity":        0,
        },

        # RandomForest hyperparameters (ExpiryRiskModel)
        # class_weight='balanced' is spec-mandated — never remove
        "random_forest": {
            "n_estimators":     300,
            "max_depth":        8,
            "class_weight":     "balanced",
            "min_samples_leaf": 5,
            "random_state":     42,
            "n_jobs":           -1,
        },
    }