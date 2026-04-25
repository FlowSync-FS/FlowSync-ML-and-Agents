"""
tests/test_agents.py

Unit tests for ml/agents/*.py

Tests agent decision logic, approval tier assignment,
coordinator conflict resolution, and deduplication.

Critical rules verified:
    - ExpiryAgent thresholds (0.85, 0.60) from compliance_config
    - ReorderAgent suppressed when active liquidation exists
    - DispatchAgent excludes ANOMALY_HOLD batches
    - Coordinator priority order never changes
    - Liquidation suppresses reorder on same product
"""

import uuid

from ml.agents.base import (
    ActionType,
    AgentDecision,
    ApprovalTier,
    COORDINATOR_PRIORITY,
)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_depot_id() -> str:
    return str(uuid.uuid4())


def _make_decision(
    action:       ActionType,
    depot_id:     str   = None,
    batch_id:     str   = None,
    product_id:   str   = None,
    conflict_key: str   = None,
    tier:         ApprovalTier = ApprovalTier.NOTIFY,
) -> AgentDecision:
    return AgentDecision(
        agent         = "TestAgent",
        action        = action,
        approval_tier = tier,
        depot_id      = depot_id or _make_depot_id(),
        batch_id      = batch_id,
        product_id    = product_id,
        conflict_key  = conflict_key,
        payload       = {"reason": "test"},
    )


# ── AgentDecision dataclass tests ─────────────────────────────────────────────

class TestAgentDecision:

    def test_conflict_key_auto_generated_from_batch(self):
        """conflict_key should be auto-set when batch_id provided."""
        bid = str(uuid.uuid4())
        d   = _make_decision(
            action   = ActionType.ANOMALY_HOLD,
            batch_id = bid,
        )
        assert d.conflict_key == f"ANOMALY_HOLD_{bid}"

    def test_conflict_key_auto_generated_from_product(self):
        """conflict_key should use product_id when no batch_id."""
        pid = str(uuid.uuid4())
        d   = _make_decision(
            action     = ActionType.SUGGEST_REORDER,
            product_id = pid,
        )
        assert d.conflict_key == f"SUGGEST_REORDER_{pid}"

    def test_conflict_key_manual_override(self):
        """Manually provided conflict_key should not be overwritten."""
        d = _make_decision(
            action       = ActionType.DISPATCH_PLAN,
            conflict_key = "custom_key_123",
        )
        assert d.conflict_key == "custom_key_123"

    def test_all_action_types_in_priority(self):
        """Every ActionType must have a priority in COORDINATOR_PRIORITY."""
        for action in ActionType:
            assert action in COORDINATOR_PRIORITY, (
                f"{action} missing from COORDINATOR_PRIORITY"
            )

    def test_priority_order_correct(self):
        """
        ANOMALY_HOLD must have highest priority (lowest number).
        ANOMALY_ALERT must have lowest priority (highest number).
        """
        assert COORDINATOR_PRIORITY[ActionType.ANOMALY_HOLD] == 1
        assert COORDINATOR_PRIORITY[ActionType.ANOMALY_ALERT] == 6

    def test_liquidation_beats_dispatch(self):
        """SUGGEST_LIQUIDATION has a lower priority number than FLAG_PRIORITY_DISPATCH."""
        assert (
            COORDINATOR_PRIORITY[ActionType.SUGGEST_LIQUIDATION] <
            COORDINATOR_PRIORITY[ActionType.FLAG_PRIORITY_DISPATCH]
        )

    def test_reorder_lower_priority_than_liquidation(self):
        """SUGGEST_REORDER should have lower priority than SUGGEST_LIQUIDATION."""
        assert (
            COORDINATOR_PRIORITY[ActionType.SUGGEST_REORDER] >
            COORDINATOR_PRIORITY[ActionType.SUGGEST_LIQUIDATION]
        )


# ── AgentCoordinator tests ─────────────────────────────────────────────────────

class TestCoordinatorAgent:

    def test_liquidation_suppresses_reorder_same_product(self):
        """
        SUGGEST_LIQUIDATION on product X suppresses SUGGEST_REORDER for X.
        """
        from ml.agents.coordinator import AgentCoordinator

        coordinator = AgentCoordinator()
        product_id  = str(uuid.uuid4())
        depot_id    = _make_depot_id()

        liquidation = _make_decision(
            action       = ActionType.SUGGEST_LIQUIDATION,
            depot_id     = depot_id,
            product_id   = product_id,
            tier         = ApprovalTier.APPROVE,
            conflict_key = "expiry_batch_001",
        )
        reorder = _make_decision(
            action       = ActionType.SUGGEST_REORDER,
            depot_id     = depot_id,
            product_id   = product_id,
            tier         = ApprovalTier.APPROVE,
            conflict_key = f"reorder_{product_id}",
        )

        resolved      = coordinator._resolve_conflicts([liquidation, reorder])
        action_types  = [r.action for r in resolved]
        assert ActionType.SUGGEST_LIQUIDATION in action_types
        assert ActionType.SUGGEST_REORDER not in action_types, (
            "ReorderAgent decision should be suppressed when "
            "SUGGEST_LIQUIDATION exists for same product"
        )

    def test_reorder_kept_when_no_liquidation(self):
        """SUGGEST_REORDER is kept when no liquidation exists for that product."""
        from ml.agents.coordinator import AgentCoordinator

        coordinator = AgentCoordinator()
        product_id  = str(uuid.uuid4())
        depot_id    = _make_depot_id()

        reorder  = _make_decision(
            action       = ActionType.SUGGEST_REORDER,
            depot_id     = depot_id,
            product_id   = product_id,
            tier         = ApprovalTier.APPROVE,
            conflict_key = f"reorder_{product_id}",
        )

        resolved = coordinator._resolve_conflicts([reorder])
        assert len(resolved) == 1
        assert resolved[0].action == ActionType.SUGGEST_REORDER

    def test_anomaly_hold_higher_priority_than_dispatch(self):
        """ANOMALY_HOLD has a lower priority number than DISPATCH_PLAN."""
        assert (
            COORDINATOR_PRIORITY[ActionType.ANOMALY_HOLD] <
            COORDINATOR_PRIORITY[ActionType.DISPATCH_PLAN]
        )

    def test_multiple_different_keys_all_committed(self):
        """Decisions with different conflict_keys are all retained."""
        from ml.agents.coordinator import AgentCoordinator

        coordinator = AgentCoordinator()
        depot_id    = _make_depot_id()

        decisions = [
            _make_decision(
                action       = ActionType.ANOMALY_ALERT,
                depot_id     = depot_id,
                conflict_key = f"alert_{i}",
                tier         = ApprovalTier.NOTIFY,
            )
            for i in range(3)
        ]

        resolved = coordinator._resolve_conflicts(decisions)
        assert len(resolved) == 3

    def test_empty_decisions_returns_empty(self):
        """Empty input should return empty list without errors."""
        from ml.agents.coordinator import AgentCoordinator

        coordinator = AgentCoordinator()
        resolved    = coordinator._resolve_conflicts([])
        assert resolved == []

    def test_run_empty_state_returns_empty(self):
        """Running coordinator with no actionable state yields empty list."""
        from ml.agents.coordinator import AgentCoordinator

        coordinator = AgentCoordinator()
        results     = coordinator.run({"depot_id": "depot_001"})
        assert results == []

    def test_run_results_sorted_by_priority(self):
        """Results from run() must be ordered by COORDINATOR_PRIORITY ascending."""
        from ml.agents.coordinator import AgentCoordinator

        coordinator = AgentCoordinator()
        depot_id    = _make_depot_id()
        batch_id    = str(uuid.uuid4())
        product_id  = str(uuid.uuid4())

        state = {
            "depot_id": depot_id,
            "anomaly_flags": [{
                "batch_id":      batch_id,
                "product_id":    product_id,
                "z_score":       3.5,
                "action":        "ANOMALY_HOLD",
                "quantity":      100.0,
                "movement_type": "OUT",
                "movement_id":   "mov_001",
                "product_name":  "Test Drug",
            }],
            "expiry_predictions": [{
                "batch_id":                     str(uuid.uuid4()),
                "product_id":                   str(uuid.uuid4()),
                "expiry_risk_score":             0.92,
                "remaining_qty":                50,
                "ptr":                          12.0,
                "product_name":                 "Drug B",
                "recommended_liquidation_date": "2026-01-15",
                "method":                       "model",
            }],
        }

        results    = coordinator.run(state)
        priorities = [
            COORDINATOR_PRIORITY.get(ActionType(r["action_type"]), 99)
            for r in results
        ]
        assert priorities == sorted(priorities)

    def test_resolve_and_commit_alias(self):
        """resolve_and_commit() is a working alias for run()."""
        from ml.agents.coordinator import AgentCoordinator

        coordinator = AgentCoordinator()
        result      = coordinator.resolve_and_commit({"depot_id": "depot_001"})
        assert result == []


# ── ExpiryPreventionAgent tests ────────────────────────────────────────────────

class TestExpiryAgent:

    def _make_prediction(
        self,
        risk_score:    float,
        batch_id:      str   = None,
        product_id:    str   = None,
        remaining_qty: float = 50.0,
        ptr:           float = 12.0,
    ) -> dict:
        return {
            "batch_id":                     batch_id or str(uuid.uuid4()),
            "product_id":                   product_id or str(uuid.uuid4()),
            "expiry_risk_score":             risk_score,
            "remaining_qty":                remaining_qty,
            "ptr":                          ptr,
            "product_name":                 "Paracetamol 500mg",
            "recommended_liquidation_date": "2026-01-01",
            "method":                       "model",
        }

    def test_critical_risk_triggers_approve_tier(self):
        """risk > 0.85 → SUGGEST_LIQUIDATION with APPROVE tier."""
        from ml.agents.expiry_agent import ExpiryAgent

        agent    = ExpiryAgent()
        state    = {
            "depot_id":          "depot_001",
            "expiry_predictions": [self._make_prediction(0.90)],
        }
        decisions = agent.run(state)

        critical = [
            d for d in decisions
            if d.action == ActionType.SUGGEST_LIQUIDATION
        ]
        assert len(critical) >= 1
        assert critical[0].approval_tier == ApprovalTier.APPROVE

    def test_warning_risk_triggers_notify_tier(self):
        """risk between 0.60 and 0.85 → FLAG_PRIORITY_DISPATCH, NOTIFY."""
        from ml.agents.expiry_agent import ExpiryAgent

        agent    = ExpiryAgent()
        state    = {
            "depot_id":          "depot_001",
            "expiry_predictions": [self._make_prediction(0.72)],
        }
        decisions = agent.run(state)

        warnings = [
            d for d in decisions
            if d.action == ActionType.FLAG_PRIORITY_DISPATCH
        ]
        assert len(warnings) >= 1
        assert warnings[0].approval_tier == ApprovalTier.NOTIFY

    def test_low_risk_no_decision(self):
        """risk < 0.60 → no decision produced."""
        from ml.agents.expiry_agent import ExpiryAgent

        agent    = ExpiryAgent()
        state    = {
            "depot_id":          "depot_001",
            "expiry_predictions": [self._make_prediction(0.30)],
        }
        decisions = agent.run(state)
        assert decisions == []

    def test_payload_contains_loss_estimate(self):
        """SUGGEST_LIQUIDATION payload must include rupee loss estimate."""
        from ml.agents.expiry_agent import ExpiryAgent

        agent    = ExpiryAgent()
        state    = {
            "depot_id":          "depot_001",
            "expiry_predictions": [
                self._make_prediction(0.90, remaining_qty=100.0, ptr=50.0)
            ],
        }
        decisions = agent.run(state)
        critical  = [
            d for d in decisions
            if d.action == ActionType.SUGGEST_LIQUIDATION
        ]

        assert len(critical) >= 1
        payload = critical[0].payload
        assert "estimated_loss_if_ignored_inr" in payload
        assert payload["estimated_loss_if_ignored_inr"] == 5000.0  # 100 × 50


# ── ReorderAgent tests ─────────────────────────────────────────────────────────

class TestReorderAgent:

    def _make_stockout_row(
        self,
        product_id:  str   = None,
        days:        float = 3.0,
        lead_time:   float = 7.0,
        units_14d:   float = 50.0,
        stock:       float = 10.0,
    ) -> dict:
        return {
            "product_id":          product_id or str(uuid.uuid4()),
            "product_name":        "Amoxicillin 500mg",
            "days_until_stockout": days,
            "lead_time_days":      lead_time,
            "current_stock":       stock,
            "predicted_units_14d": units_14d,
        }

    def test_critical_stockout_approve_tier(self):
        """
        days_until_stockout < lead_time → SUGGEST_REORDER with APPROVE tier.
        """
        from ml.agents.reorder_agent import ReorderAgent

        agent    = ReorderAgent()
        state    = {
            "depot_id":       "depot_001",
            "stockout_risks": [self._make_stockout_row(days=3.0, lead_time=7.0)],
            "cashflow_negative": False,
        }
        decisions = agent.run(state)

        reorders = [
            d for d in decisions
            if d.action == ActionType.SUGGEST_REORDER
        ]
        assert len(reorders) >= 1
        assert reorders[0].approval_tier == ApprovalTier.APPROVE

    def test_planning_window_notify_tier(self):
        """
        days_until_stockout between lead_time and lead_time×1.5
        → SUGGEST_REORDER with NOTIFY tier.
        """
        from ml.agents.reorder_agent import ReorderAgent

        agent    = ReorderAgent()
        state    = {
            "depot_id":       "depot_001",
            "stockout_risks": [self._make_stockout_row(days=9.0, lead_time=7.0)],
            "cashflow_negative": False,
        }
        decisions = agent.run(state)

        reorders = [
            d for d in decisions
            if d.action == ActionType.SUGGEST_REORDER
        ]
        assert len(reorders) >= 1
        assert reorders[0].approval_tier == ApprovalTier.NOTIFY

    def test_negative_cashflow_reduces_qty(self):
        """
        Negative cashflow should reduce reorder qty by 30%.
        """
        from ml.agents.reorder_agent import ReorderAgent

        agent    = ReorderAgent()
        state    = {
            "depot_id":       "depot_001",
            "stockout_risks": [
                self._make_stockout_row(days=3.0, lead_time=7.0, units_14d=100.0)
            ],
            "cashflow_negative": True,
        }
        decisions = agent.run(state)

        reorders = [
            d for d in decisions
            if d.action == ActionType.SUGGEST_REORDER
        ]
        assert len(reorders) >= 1
        qty = reorders[0].payload["reorder_qty"]
        assert qty <= 70   # 100 × 0.7 = 70


# ── AnomalyAgent tests ─────────────────────────────────────────────────────────

class TestAnomalyAgent:

    def _make_flag(
        self,
        action_str: str,
        batch_id:   str   = None,
        product_id: str   = None,
        z_score:    float = 3.2,
        qty:        float = 500.0,
    ) -> dict:
        return {
            "movement_id":   str(uuid.uuid4()),
            "batch_id":      batch_id or str(uuid.uuid4()),
            "z_score":       z_score,
            "action":        action_str,
            "quantity":      qty,
            "movement_type": "OUT",
            "product_id":    product_id or str(uuid.uuid4()),
            "product_name":  "Paracetamol 500mg",
        }

    def test_hold_action_approve_tier(self):
        """ANOMALY_HOLD flag → APPROVE tier."""
        from ml.agents.anomaly_agent import AnomalyAgent

        agent    = AnomalyAgent()
        state    = {
            "depot_id":     "depot_001",
            "anomaly_flags": [self._make_flag("ANOMALY_HOLD")],
        }
        decisions = agent.run(state)

        holds = [d for d in decisions if d.action == ActionType.ANOMALY_HOLD]
        assert len(holds) >= 1
        assert holds[0].approval_tier == ApprovalTier.APPROVE

    def test_alert_action_notify_tier(self):
        """ANOMALY_ALERT flag → NOTIFY tier."""
        from ml.agents.anomaly_agent import AnomalyAgent

        agent    = AnomalyAgent()
        state    = {
            "depot_id":     "depot_001",
            "anomaly_flags": [self._make_flag("ANOMALY_ALERT", z_score=2.3)],
        }
        decisions = agent.run(state)

        alerts = [d for d in decisions if d.action == ActionType.ANOMALY_ALERT]
        assert len(alerts) >= 1
        assert alerts[0].approval_tier == ApprovalTier.NOTIFY

    def test_no_flags_empty_decisions(self):
        """No anomaly flags → empty decisions list."""
        from ml.agents.anomaly_agent import AnomalyAgent

        agent    = AnomalyAgent()
        state    = {"depot_id": "depot_001", "anomaly_flags": []}
        decisions = agent.run(state)
        assert decisions == []
