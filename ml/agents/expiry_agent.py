"""
ml/agents/expiry_agent.py

ExpiryPreventionAgent — in-memory BaseAgent.
Consumes state["expiry_predictions"] (list of dicts, pre-computed at Stage 2).

Two decisions:
    risk > critical (0.85) → SUGGEST_LIQUIDATION (APPROVE) — manager must confirm
    risk > warning  (0.60) → FLAG_PRIORITY_DISPATCH (NOTIFY) — WhatsApp fires, proceeds

Thresholds read from config (never hardcoded).
conflict_key = expiry_{batch_id} — prevents duplicate alerts.
"""

import logging
from typing import List

from ml.agents.base import (
    AgentDecision,
    ActionType,
    ApprovalTier,
    BaseAgent,
)

logger = logging.getLogger("flowsync.agents.expiry")

_DEFAULT_CRITICAL = 0.85
_DEFAULT_WARNING  = 0.60


class ExpiryAgent(BaseAgent):
    """
    Converts expiry risk scores into liquidation or priority-dispatch decisions.

    Expects state dict keys:
        depot_id           (str)
        expiry_predictions (list[dict]) — each dict: batch_id, product_id,
                           expiry_risk_score, recommended_liquidation_date,
                           method, remaining_qty, ptr, product_name
    """

    name = "expiry"

    def observe(self, state: dict) -> dict:
        """
        Extract expiry predictions and thresholds from shared state.

        Args:
            state: Shared orchestrator state dict.

        Returns:
            Dict with depot_id, predictions, and threshold values.
        """
        return {
            "depot_id":    state.get("depot_id", ""),
            "predictions": state.get("expiry_predictions", []),
            "critical":    self.config.get("critical_threshold", _DEFAULT_CRITICAL),
            "warning":     self.config.get("warning_threshold", _DEFAULT_WARNING),
        }

    def act(self, observations: dict) -> List[AgentDecision]:
        """
        Emit SUGGEST_LIQUIDATION or FLAG_PRIORITY_DISPATCH per batch.

        Args:
            observations: Output of observe().

        Returns:
            List of AgentDecision objects.
        """
        depot_id = observations["depot_id"]
        critical = observations["critical"]
        warning  = observations["warning"]
        decisions: List[AgentDecision] = []

        for row in observations["predictions"]:
            risk         = float(row.get("expiry_risk_score", 0))
            bid          = str(row.get("batch_id", ""))
            remaining    = float(row.get("remaining_qty", 0))
            ptr          = float(row.get("ptr", 0))
            loss_est     = round(remaining * ptr, 2)
            product_name = str(row.get("product_name", "Unknown"))
            liq_date     = str(row.get("recommended_liquidation_date", ""))
            product_id   = str(row.get("product_id", ""))
            method       = str(row.get("method", ""))

            if risk >= critical:
                decisions.append(AgentDecision(
                    agent         = "ExpiryPreventionAgent",
                    action        = ActionType.SUGGEST_LIQUIDATION,
                    approval_tier = ApprovalTier.APPROVE,
                    depot_id      = depot_id,
                    batch_id      = bid,
                    product_id    = product_id,
                    conflict_key  = f"expiry_{bid}",
                    payload       = {
                        "risk_score":                    risk,
                        "recommended_liquidation_date":  liq_date,
                        "estimated_loss_if_ignored_inr": loss_est,
                        "remaining_qty":                 remaining,
                        "product_name":                  product_name,
                        "method":                        method,
                        "reason": (
                            f"{product_name}: expiry risk {risk:.0%} — critical. "
                            f"Liquidate by {liq_date} "
                            f"or lose ₹{loss_est:,.0f}."
                        ),
                    },
                    metadata={"raw_score": risk},
                ))

            elif risk >= warning:
                decisions.append(AgentDecision(
                    agent         = "ExpiryPreventionAgent",
                    action        = ActionType.FLAG_PRIORITY_DISPATCH,
                    approval_tier = ApprovalTier.NOTIFY,
                    depot_id      = depot_id,
                    batch_id      = bid,
                    product_id    = product_id,
                    conflict_key  = f"expiry_{bid}",
                    payload       = {
                        "risk_score":   risk,
                        "product_name": product_name,
                        "liq_date":     liq_date,
                        "reason": (
                            f"{product_name}: expiry risk {risk:.0%} — "
                            "priority dispatch recommended."
                        ),
                    },
                    metadata={"raw_score": risk},
                ))

        logger.info(
            f"[{depot_id}] ExpiryAgent: {len(decisions)} decisions "
            f"(critical={critical}, warning={warning})"
        )
        return decisions
