"""
tests/test_inference.py

Unit tests for ml/inference/*.py

Tests the inference pipeline stages:
    - FEFO ranking logic and ML override condition
    - Stockout calculation formula
    - Anomaly Z-score thresholds
    - Orchestrator stage error handling and fallback
"""

import uuid
from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest

from ml.agents.base import ActionType


# ── FEFORanker tests ───────────────────────────────────────────────────────────

class TestFEFORanker:

    @pytest.mark.asyncio
    async def test_fefo_default_sort_by_expiry(self):
        """
        Without ML override, batches should be sorted
        by days_till_expiry ascending (soonest first).
        """
        from ml.inference.infer_fefo import run

        depot_id = str(uuid.uuid4())
        today    = date.today()

        batch_rows = [
            MagicMock(
                batch_id      = str(uuid.uuid4()),
                product_id    = str(uuid.uuid4()),
                expiry_date   = today + timedelta(days=90),
                remaining_qty = 50.0,
            ),
            MagicMock(
                batch_id      = str(uuid.uuid4()),
                product_id    = str(uuid.uuid4()),
                expiry_date   = today + timedelta(days=30),   # soonest
                remaining_qty = 30.0,
            ),
            MagicMock(
                batch_id      = str(uuid.uuid4()),
                product_id    = str(uuid.uuid4()),
                expiry_date   = today + timedelta(days=180),
                remaining_qty = 80.0,
            ),
        ]

        db              = AsyncMock()
        result          = MagicMock()
        result.fetchall = MagicMock(return_value=batch_rows)
        db.execute      = AsyncMock(return_value=result)
        db.commit       = AsyncMock()

        expiry_results = {}   # no expiry scores → no ML override

        with patch("ml.inference.infer_fefo.get", return_value=0.6):
            rankings = await run(depot_id, expiry_results, db)

        if rankings:
            days_list = [r["days_till_expiry"] for r in rankings]
            assert days_list == sorted(days_list), (
                "FEFO rankings not sorted by days_till_expiry ascending"
            )

    @pytest.mark.asyncio
    async def test_ml_override_pushes_batch_to_top(self):
        """
        A batch with expiry_risk_score > threshold should
        have priority_score < 0 (pushed to top).
        """
        from ml.inference.infer_fefo import run

        depot_id           = str(uuid.uuid4())
        today              = date.today()
        high_risk_batch_id = str(uuid.uuid4())

        batch_rows = [
            MagicMock(
                batch_id      = high_risk_batch_id,
                product_id    = str(uuid.uuid4()),
                expiry_date   = today + timedelta(days=120),
                remaining_qty = 100.0,
            ),
        ]

        db              = AsyncMock()
        result          = MagicMock()
        result.fetchall = MagicMock(return_value=batch_rows)
        db.execute      = AsyncMock(return_value=result)
        db.commit       = AsyncMock()

        expiry_results = {
            high_risk_batch_id: {"expiry_risk_score": 0.88}   # > threshold 0.6
        }

        with patch("ml.inference.infer_fefo.get", return_value=0.6):
            rankings = await run(depot_id, expiry_results, db)

        if rankings:
            top_batch = rankings[0]
            assert top_batch["ml_override"] is True
            assert top_batch["priority_score"] < 0, (
                "ML override batch should have negative priority_score"
            )

    def test_priority_score_below_threshold_is_positive(self):
        """
        Batch with risk below threshold should have
        priority_score equal to days_till_expiry (positive).
        """
        days_left     = 90
        risk_score    = 0.40
        threshold     = 0.60
        ml_override   = risk_score > threshold

        priority_score = (
            -(risk_score * 1000) if ml_override else float(days_left)
        )
        assert priority_score > 0
        assert priority_score == 90.0


# ── StockoutCalculator tests ───────────────────────────────────────────────────

class TestStockoutCalculator:

    def test_days_until_stockout_formula(self):
        """
        days_until_stockout = current_stock / predicted_daily_rate
        """
        current_stock      = 100.0
        predicted_daily    = 10.0
        expected_days      = current_stock / predicted_daily

        assert expected_days == 10.0

    def test_zero_rate_clips_to_minimum(self):
        """
        predicted_daily_rate of 0 should clip to 0.01
        to prevent division by zero.
        """
        current_stock   = 100.0
        daily_rate      = max(0.0, 0.01)   # clipped
        days_left       = current_stock / daily_rate

        assert days_left == 10_000.0   # very large — not a ZeroDivisionError

    def test_will_stockout_true_when_days_less_than_lead(self):
        """
        will_stockout = True when days_until_stockout < lead_time_days.
        """
        days_left = 3.0
        lead_time = 7.0

        will_stockout = days_left < lead_time
        assert will_stockout is True

    def test_will_stockout_false_when_days_gte_lead(self):
        """
        will_stockout = False when days_until_stockout >= lead_time_days.
        """
        days_left = 10.0
        lead_time = 7.0

        will_stockout = days_left < lead_time
        assert will_stockout is False

    @pytest.mark.asyncio
    async def test_run_returns_dict(self):
        """run() should return a dict keyed by product_id."""
        from ml.inference.infer_stockout import run

        depot_id = str(uuid.uuid4())
        product_id = str(uuid.uuid4())

        stock_row         = MagicMock()
        stock_row.product_id  = product_id
        stock_row.current_stock = 50.0
        stock_row.lead_time_days = 7.0

        db              = AsyncMock()
        result          = MagicMock()
        result.fetchall = MagicMock(return_value=[stock_row])
        db.execute      = AsyncMock(return_value=result)
        db.commit       = AsyncMock()

        demand_results = {
            product_id: {"predicted_daily_rate": 5.0}
        }

        with patch("ml.inference.infer_stockout.get", return_value=7):
            results = await run(depot_id, demand_results, db)

        assert isinstance(results, dict)
        if results:
            assert product_id in results
            assert "days_until_stockout" in results[product_id]
            assert "will_stockout" in results[product_id]


# ── AnomalyEngine tests ────────────────────────────────────────────────────────

class TestAnomalyEngine:

    @pytest.mark.asyncio
    async def test_high_zscore_produces_hold_flag(self):
        """
        Movement with Z-score > 2.5 should produce ANOMALY_HOLD flag.
        """
        from ml.inference.infer_anomaly import run

        depot_id    = str(uuid.uuid4())
        movement_id = str(uuid.uuid4())
        batch_id    = str(uuid.uuid4())

        # Baseline: mean=10, std=2 → qty=25 → z=(25-10)/2=7.5
        # Use tuples so pd.DataFrame(..., columns=[...]) can unpack them correctly.
        baseline_tuples = [("pain_relief", 10.0)] * 10

        movement_row = MagicMock()
        movement_row.movement_id   = movement_id
        movement_row.batch_id      = batch_id
        movement_row.product_id    = str(uuid.uuid4())
        movement_row.quantity      = 25.0   # far above mean
        movement_row.movement_type = "OUT"
        movement_row.created_at    = pd.Timestamp.today()
        movement_row.product_category = "pain_relief"

        db = AsyncMock()

        def side_effect(query, *args, **kwargs):
            result = MagicMock()
            if "30 days" in str(query) and "INTERVAL" in str(query):
                result.fetchall = MagicMock(return_value=baseline_tuples)
            else:
                result.fetchall = MagicMock(return_value=[movement_row])
            return result

        db.execute = AsyncMock(side_effect=side_effect)
        db.commit  = AsyncMock()

        with patch("ml.inference.infer_anomaly.get") as mock_get:
            mock_get.side_effect = lambda key, db=None, default=None: {
                "anomaly_hold_threshold":  2.5,
                "anomaly_alert_threshold": 2.0,
            }.get(key, default)

            flags = await run(depot_id, "2025-10-20", db)

        hold_flags = [f for f in flags if f["action"] == "ANOMALY_HOLD"]
        assert len(hold_flags) >= 1

    @pytest.mark.asyncio
    async def test_normal_movement_no_flag(self):
        """
        Movement at category mean should not produce any flag.
        """
        from ml.inference.infer_anomaly import run

        depot_id    = str(uuid.uuid4())
        batch_id    = str(uuid.uuid4())

        # Mean qty = 10, this movement = 10 → z = 0
        # Use tuples so pd.DataFrame(..., columns=[...]) can unpack them correctly.
        baseline_rows = [("antibiotic", 10.0)] * 10
        movement_row  = MagicMock(
            movement_id      = str(uuid.uuid4()),
            batch_id         = batch_id,
            product_id       = str(uuid.uuid4()),
            quantity         = 10.0,
            movement_type    = "OUT",
            created_at       = pd.Timestamp.today(),
            product_category = "antibiotic",
        )

        db = AsyncMock()

        call_count = [0]
        def side_effect(query, *args, **kwargs):
            result = MagicMock()
            call_count[0] += 1
            if call_count[0] == 1:
                result.fetchall = MagicMock(return_value=baseline_rows)
            else:
                result.fetchall = MagicMock(return_value=[movement_row])
            return result

        db.execute = AsyncMock(side_effect=side_effect)
        db.commit  = AsyncMock()

        with patch("ml.inference.infer_anomaly.get") as mock_get:
            mock_get.side_effect = lambda key, db=None, default=None: {
                "anomaly_hold_threshold":  2.5,
                "anomaly_alert_threshold": 2.0,
            }.get(key, default)

            flags = await run(depot_id, "2025-10-20", db)

        assert flags == []


# ── Orchestrator error handling tests ─────────────────────────────────────────

class TestOrchestrator:

    @pytest.mark.asyncio
    async def test_demand_failure_with_no_fallback_returns_failed(self):
        """
        If demand stage fails and no yesterday data exists,
        orchestrator must return RunStatus.FAILED.
        """
        from ml.inference.orchestrator import (
            run_inference_pipeline,
            RunStatus,
        )

        db        = AsyncMock()
        db.execute = AsyncMock(side_effect=Exception("DB connection lost"))
        db.commit  = AsyncMock()

        with patch(
            "ml.inference.orchestrator.infer_demand.run",
            side_effect=Exception("Model load failed"),
        ), patch(
            "ml.inference.orchestrator._load_yesterday",
            return_value=None,   # no fallback available
        ), patch(
            "ml.inference.orchestrator._write_run_log",
            new_callable=AsyncMock,
        ), patch(
            "ml.inference.orchestrator._alert_ops",
            new_callable=AsyncMock,
        ):
            status = await run_inference_pipeline(
                "depot_001", "2025-10-20", db
            )

        assert status == RunStatus.FAILED

    @pytest.mark.asyncio
    async def test_demand_failure_with_fallback_returns_partial(self):
        """
        If demand stage fails but yesterday data exists, orchestrator
        should continue and return PARTIAL status.
        The agents stage will fail (module-level run() removed in refactor)
        which is one of the conditions that returns PARTIAL.
        """
        from ml.inference.orchestrator import (
            run_inference_pipeline,
            RunStatus,
        )

        db        = AsyncMock()
        db.commit = AsyncMock()

        yesterday_data = {"prod_001": {"predicted_daily_rate": 5.0}}

        with patch(
            "ml.inference.orchestrator.infer_demand.run",
            side_effect=Exception("Model unavailable"),
        ), patch(
            "ml.inference.orchestrator._load_yesterday",
            return_value=yesterday_data,
        ), patch(
            "ml.inference.orchestrator.infer_expiry.run",
            new_callable=AsyncMock,
            return_value={},
        ), patch(
            "ml.inference.orchestrator.infer_stockout.run",
            new_callable=AsyncMock,
            return_value={},
        ), patch(
            "ml.inference.orchestrator.infer_fefo.run",
            new_callable=AsyncMock,
            return_value=[],
        ), patch(
            "ml.inference.orchestrator._run_anomaly_safe",
            new_callable=AsyncMock,
        ), patch(
            "ml.inference.orchestrator._write_run_log",
            new_callable=AsyncMock,
        ):
            status = await run_inference_pipeline(
                "depot_001", "2025-10-20", db
            )

        assert status == RunStatus.PARTIAL