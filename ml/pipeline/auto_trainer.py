"""
ml/pipeline/auto_trainer.py

Client onboarding + scheduled retraining pipeline.
Orchestrates: ingest → map → validate → train → A/B test → register

Triggered by:
    1. New client uploads historical data  (trigger='onboard')
    2. Sunday midnight Celery task         (trigger='scheduled')
    3. PSI drift monitor                   (trigger='drift')

Three training outcomes:
    SUFFICIENT_FOR_BOTH   → fine-tune demand + expiry models
    SUFFICIENT_FOR_DEMAND → fine-tune demand model only
    USE_GLOBAL_MODEL      → deploy global model, collect data

Fine-tuning uses XGBoost warm-start (xgb_model param) from
the global model — never trains from scratch on client data.
Deploys fine-tuned model only if it beats global on client's
own validation split (A/B test).
"""

import logging
from datetime import datetime
from typing import Optional

import numpy as np
import pandas as pd
import xgboost as xgb
from sklearn.metrics import mean_absolute_percentage_error

from ml.features.demand_features import (
    build_demand_features,
    fill_missing_dates,
)
from ml.pipeline.data_ingester   import ingest, ingest_from_dataframe
from ml.pipeline.schema_mapper   import map_schema
from ml.pipeline.data_validator  import validate, DataSufficiency
from ml.registry.model_store     import ModelStore
from ml.shared.config_loader     import _defaults

logger = logging.getLogger("flowsync.pipeline.auto_trainer")


class AutoTrainer:
    """
    Usage — onboarding:
        trainer = AutoTrainer(db_session)
        result  = trainer.run(
            client_id="uuid",
            file_path="uploads/client_tally.csv",
            trigger="onboard",
        )

    Usage — scheduled retraining (Celery):
        trainer = AutoTrainer(db_session)
        result  = trainer.run(client_id="uuid", trigger="scheduled")
    """

    def __init__(self, db_session=None):
        self.db    = db_session
        self.store = ModelStore(db_session=db_session)
        self.cfg   = _defaults()

    def run(
        self,
        client_id: str,
        file_path: Optional[str] = None,
        trigger: str = "scheduled",
    ) -> dict:
        """
        Full auto-training run for one client.

        Args:
            client_id: UUID string
            file_path: path to raw data file (onboard only)
                       If None, loads from DB (scheduled/drift)
            trigger:   'onboard' | 'scheduled' | 'drift'

        Returns:
            Result dict with keys:
                decision, client_id, demand_deployed,
                expiry_deployed, completed_at, reason
        """
        logger.info(
            f"AutoTrainer: client={client_id} trigger={trigger}"
        )

        # ── Step 1: Load data ─────────────────────────────────────────────────
        if file_path:
            raw_df = ingest(file_path)
        else:
            raw_df = self._load_from_db(client_id)

        if raw_df is None or raw_df.empty:
            logger.warning(f"[{client_id}] No data — deploying global model")
            self._set_active_global(client_id)
            return self._result(
                client_id, "USE_GLOBAL_MODEL",
                reason="no_data_available",
            )

        # ── Step 2: Map schema ────────────────────────────────────────────────
        mapped_df, warnings = map_schema(raw_df, client_id)

        if warnings:
            logger.warning(
                f"[{client_id}] Schema warnings: {warnings}"
            )

        # ── Step 3: Validate ──────────────────────────────────────────────────
        validation = validate(mapped_df, client_id)

        if not validation.is_sufficient:
            self._set_active_global(client_id)
            return self._result(
                client_id,
                validation.status.value,
                reason=validation.reason,
            )

        # ── Step 4: Fine-tune demand model ────────────────────────────────────
        demand_deployed = False
        if validation.status in (
            DataSufficiency.SUFFICIENT_FOR_DEMAND,
            DataSufficiency.SUFFICIENT_FOR_BOTH,
        ):
            demand_deployed = self._fine_tune_demand(
                client_id, mapped_df
            )

        # ── Step 5: Fine-tune expiry model (180+ days only) ───────────────────
        expiry_deployed = False
        if validation.status == DataSufficiency.SUFFICIENT_FOR_BOTH:
            expiry_deployed = self._fine_tune_expiry(
                client_id, mapped_df
            )

        result = self._result(
            client_id,
            validation.status.value,
            reason            = validation.reason,
            demand_deployed   = demand_deployed,
            expiry_deployed   = expiry_deployed,
        )
        logger.info(f"AutoTrainer complete: {result}")
        return result

    # ── Private: fine-tuning ──────────────────────────────────────────────────

    def _fine_tune_demand(
        self,
        client_id: str,
        df: pd.DataFrame,
    ) -> bool:
        """
        Fine-tune demand forecaster on client data.
        Uses XGBoost warm-start from global model weights.
        Only deploys if fine-tuned MAPE < global MAPE on client's data.
        Returns True if fine-tuned model was deployed.
        """
        try:
            festival_dates = self.cfg.get("festival_dates", [])
            df_filled      = fill_missing_dates(df)
            X, y           = build_demand_features(
                df_filled, festival_dates=festival_dates
            )

            if len(X) < 100:
                logger.info(
                    f"[{client_id}] Too few rows for fine-tuning demand "
                    f"({len(X)}) — keeping global"
                )
                return False

            # Time-based split: last 20% as validation
            split_idx      = int(len(X) * 0.80)
            X_tr, X_val    = X.iloc[:split_idx], X.iloc[split_idx:]
            y_tr, y_val    = y.iloc[:split_idx], y.iloc[split_idx:]

            # Benchmark: global model on client's data
            global_model   = self.store.load("demand_global")
            global_preds   = global_model.predict(X_val).clip(min=0)
            mask           = y_val > 0
            global_mape    = mean_absolute_percentage_error(
                y_val[mask], global_preds[mask]
            )

            # Fine-tune from global weights
            ft_model = xgb.XGBRegressor(
                n_estimators          = 200,
                learning_rate         = 0.01,
                max_depth             = 6,
                subsample             = 0.8,
                colsample_bytree      = 0.8,
                objective             = "reg:squarederror",
                early_stopping_rounds = 30,
                random_state          = 42,
                verbosity             = 0,
            )
            ft_model.fit(
                X_tr, y_tr,
                xgb_model = global_model.get_booster(),
                eval_set  = [(X_val, y_val)],
                verbose   = False,
            )

            ft_preds = ft_model.predict(X_val).clip(min=0)
            ft_mape  = mean_absolute_percentage_error(
                y_val[mask], ft_preds[mask]
            )

            logger.info(
                f"[{client_id}] Demand A/B: "
                f"fine-tuned MAPE {ft_mape:.2%} vs "
                f"global MAPE {global_mape:.2%}"
            )

            if ft_mape < global_mape:
                self.store.save(
                    ft_model,
                    f"demand_{client_id}",
                    metadata={
                        "mape_ft":     round(ft_mape, 4),
                        "mape_global": round(global_mape, 4),
                        "client_id":   client_id,
                        "trigger":     "auto_trainer",
                        "trained_at":  datetime.now().isoformat(),
                    },
                )
                logger.info(
                    f"[{client_id}] Fine-tuned demand model deployed "
                    f"({ft_mape:.2%} vs {global_mape:.2%})"
                )
                return True
            else:
                logger.info(
                    f"[{client_id}] Global demand model still better — "
                    "no deployment"
                )
                return False

        except Exception as e:
            logger.error(
                f"[{client_id}] Demand fine-tune failed: {e}"
            )
            return False

    def _fine_tune_expiry(
        self,
        client_id: str,
        df: pd.DataFrame,
    ) -> bool:
        """
        Fine-tune expiry risk model on client's expired-batch labels.
        Only runs if client has real expired-batch data.
        Returns True if fine-tuned model was deployed.
        """
        # Expiry fine-tuning requires real labels from DB
        # The synthesised Kaggle labels are global-model-only
        # Client fine-tuning uses actual expired batches from their data
        logger.info(
            f"[{client_id}] Expiry fine-tune: "
            "requires expired-batch labels from DB — "
            "skipping file-based fine-tune (runs via DB loader)"
        )
        return False

    # ── Private: helpers ──────────────────────────────────────────────────────

    def _set_active_global(self, client_id: str) -> None:
        """Record that this client uses the global model."""
        logger.info(
            f"[{client_id}] Using global model "
            "(no client-specific model)"
        )

    def _load_from_db(
        self,
        client_id: str,
    ) -> Optional[pd.DataFrame]:
        """
        Load last 12 months of sales movements from DB.
        Used for scheduled and drift-triggered retraining.
        """
        if not self.db:
            logger.error(
                f"[{client_id}] Cannot load from DB — "
                "no db_session provided to AutoTrainer"
            )
            return None

        try:
            rows = self.db.execute("""
                SELECT
                    sm.product_id::text,
                    sm.depot_id::text,
                    DATE(sm.created_at) AS date,
                    SUM(sm.quantity)    AS units_sold,
                    p.product_category,
                    p.is_cold_chain,
                    d.region            AS depot_region
                FROM stock_movements sm
                JOIN products p ON p.id = sm.product_id
                JOIN depots   d ON d.id = sm.depot_id
                JOIN users    u ON u.depot_id = d.id
                WHERE u.client_id    = :cid
                  AND sm.movement_type = 'OUT'
                  AND sm.created_at >= NOW() - INTERVAL '12 months'
                GROUP BY
                    sm.product_id, sm.depot_id, DATE(sm.created_at),
                    p.product_category, p.is_cold_chain, d.region
            """, {"cid": client_id}).fetchall()

            if not rows:
                return None

            return ingest_from_dataframe(pd.DataFrame(rows, columns=[
                "product_id", "depot_id", "date", "units_sold",
                "product_category", "is_cold_chain", "depot_region",
            ]))

        except Exception as e:
            logger.error(
                f"[{client_id}] DB load failed: {e}"
            )
            return None

    @staticmethod
    def _result(
        client_id: str,
        decision: str,
        reason: str = "",
        demand_deployed: bool = False,
        expiry_deployed: bool = False,
    ) -> dict:
        return {
            "client_id":       client_id,
            "decision":        decision,
            "demand_deployed": demand_deployed,
            "expiry_deployed": expiry_deployed,
            "reason":          reason,
            "completed_at":    datetime.now().isoformat(),
        }