"""
ml/agents/anomaly_agent.py

AnomalyAgent — in-memory BaseAgent.
Consumes state["anomaly_flags"] (list of dicts written by infer_anomaly.py Stage 4).

Two decisions:
    action == "ANOMALY_HOLD"  → ANOMALY_HOLD  (APPROVE) — physically blocks batch
    action == "ANOMALY_ALERT" → ANOMALY_ALERT (NOTIFY)  — manager informed, nothing blocked

conflict_key = anomaly_{batch_id}
"""

import logging
from typing import List

from ml.agents.base import (
    AgentDecision,
    ActionType,
    ApprovalTier,
    BaseAgent,
)

logger = logging.getLogger("flowsync.agents.anomaly")


class AnomalyAgent(BaseAgent):
    """
    Wraps pre-computed anomaly flags into AgentDecision objects.

    Expects state dict keys:
        depot_id      (str)
        anomaly_flags (list[dict]) — each dict: batch_id, product_id, z_score,
                                     action, quantity, movement_type,
                                     movement_id, product_name
    """

    name = "anomaly"

    def observe(self, state: dict) -> dict:
        """
        Extract anomaly flags from shared state.

        Args:
            state: Shared orchestrator state dict.

        Returns:
            Dict with depot_id and anomaly_flags list.
        """
        return {
            "depot_id":     state.get("depot_id", ""),
            "anomaly_flags": state.get("anomaly_flags", []),
        }

    def act(self, observations: dict) -> List[AgentDecision]:
        """
        Convert anomaly flags into AgentDecision objects.

        Args:
            observations: Output of observe().

        Returns:
            List of AgentDecision — ANOMALY_HOLD (APPROVE) or ANOMALY_ALERT (NOTIFY).
        """
        depot_id = observations["depot_id"]
        decisions: List[AgentDecision] = []

        for flag in observations["anomaly_flags"]:
            bid          = str(flag.get("batch_id", ""))
            z            = float(flag.get("z_score", 0))
            action_str   = str(flag.get("action", ""))
            product_name = str(flag.get("product_name", "Unknown"))
            qty          = float(flag.get("quantity", 0))
            mv_type      = str(flag.get("movement_type", ""))
            mv_id        = str(flag.get("movement_id", ""))
            product_id   = str(flag.get("product_id", ""))

            is_hold = action_str == "ANOMALY_HOLD"
            action  = ActionType.ANOMALY_HOLD  if is_hold else ActionType.ANOMALY_ALERT
            tier    = ApprovalTier.APPROVE     if is_hold else ApprovalTier.NOTIFY

            decisions.append(AgentDecision(
                agent         = "AnomalyAgent",
                action        = action,
                approval_tier = tier,
                depot_id      = depot_id,
                batch_id      = bid,
                product_id    = product_id,
                conflict_key  = f"anomaly_{bid}",
                payload       = {
                    "z_score":       z,
                    "quantity":      qty,
                    "movement_type": mv_type,
                    "movement_id":   mv_id,
                    "product_name":  product_name,
                    "reason": (
                        f"{product_name}: Z-score {z:.1f} on "
                        f"{mv_type} of {qty:.0f} units — "
                        + (
                            "batch HELD pending investigation."
                            if is_hold
                            else "flagged for review."
                        )
                    ),
                },
                metadata={"z_score": z},
            ))

        hold_count  = sum(1 for d in decisions if d.action == ActionType.ANOMALY_HOLD)
        alert_count = sum(1 for d in decisions if d.action == ActionType.ANOMALY_ALERT)
        logger.info(
            f"[{depot_id}] AnomalyAgent: {hold_count} HOLDs | {alert_count} ALERTs"
        )
        return decisions
