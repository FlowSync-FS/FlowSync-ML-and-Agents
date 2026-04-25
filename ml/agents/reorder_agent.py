"""
ml/agents/reorder_agent.py

ReorderAgent — in-memory BaseAgent.
Consumes state["stockout_risks"] (list of dicts from infer_stockout.py Stage 2).

Two guards before any reorder decision:
    Guard 1: Active SUGGEST_LIQUIDATION on same product → suppress reorder
             (state["liquidation_products"] set[str] of product_ids)
    Guard 2: 14-day cashflow negative → reduce suggested qty by 30%
             (state["cashflow_negative"] bool)

Two decisions:
    days < lead_time        → SUGGEST_REORDER (APPROVE — critical urgency)
    days < lead_time × 1.5  → SUGGEST_REORDER (NOTIFY — planning window)

conflict_key = reorder_{product_id}
"""

import logging
from typing import List

from ml.agents.base import (
    AgentDecision,
    ActionType,
    ApprovalTier,
    BaseAgent,
)

logger = logging.getLogger("flowsync.agents.reorder")

_DEFAULT_CASHFLOW_REDUCTION = 0.30


class ReorderAgent(BaseAgent):
    """
    Converts stockout risk data into reorder decisions.

    Expects state dict keys:
        depot_id             (str)
        stockout_risks       (list[dict]) — each dict: product_id, product_name,
                             days_until_stockout, lead_time_days,
                             current_stock, predicted_units_14d
        liquidation_products (list[str] | set[str]) — product_ids with active liquidation
        cashflow_negative    (bool) — True if 14-day net cashflow < 0
    """

    name = "reorder"

    def observe(self, state: dict) -> dict:
        """
        Extract stockout risks and contextual guards from shared state.

        Args:
            state: Shared orchestrator state dict.

        Returns:
            Dict with depot_id, stockout_risks, liquidation_products, cashflow_negative.
        """
        return {
            "depot_id":             state.get("depot_id", ""),
            "stockout_risks":       state.get("stockout_risks", []),
            "liquidation_products": set(state.get("liquidation_products", [])),
            "cashflow_negative":    bool(state.get("cashflow_negative", False)),
            "cashflow_reduction":   self.config.get(
                "cashflow_reduction", _DEFAULT_CASHFLOW_REDUCTION
            ),
        }

    def act(self, observations: dict) -> List[AgentDecision]:
        """
        Emit SUGGEST_REORDER decisions, applying both guards.

        Args:
            observations: Output of observe().

        Returns:
            List of AgentDecision with action=SUGGEST_REORDER (APPROVE or NOTIFY).
        """
        depot_id             = observations["depot_id"]
        liquidation_products = observations["liquidation_products"]
        cashflow_negative    = observations["cashflow_negative"]
        reduction            = observations["cashflow_reduction"]
        decisions: List[AgentDecision] = []

        if cashflow_negative:
            logger.info(
                f"[{depot_id}] ReorderAgent: cashflow negative — "
                f"qty reduced {reduction:.0%}"
            )

        for row in observations["stockout_risks"]:
            pid          = str(row.get("product_id", ""))
            days_left    = float(row.get("days_until_stockout", 0))
            lead_time    = float(row.get("lead_time_days", 7))
            units_14d    = float(row.get("predicted_units_14d", 0))
            product_name = str(row.get("product_name", "Unknown"))

            # Guard 1: suppress reorder when liquidation is active for this product
            if pid in liquidation_products:
                logger.debug(
                    f"[{depot_id}] Reorder suppressed for {pid} "
                    "— active liquidation exists"
                )
                continue

            # Only act within planning window (< 1.5 × lead_time)
            if days_left >= lead_time * 1.5:
                continue

            # Base reorder quantity from 14-day forecast
            reorder_qty = max(int(units_14d), 1)
            if cashflow_negative:
                reorder_qty = max(int(reorder_qty * (1 - reduction)), 1)

            if days_left < lead_time:
                tier    = ApprovalTier.APPROVE
                urgency = "CRITICAL"
            else:
                tier    = ApprovalTier.NOTIFY
                urgency = "NORMAL"

            decisions.append(AgentDecision(
                agent         = "ReorderAgent",
                action        = ActionType.SUGGEST_REORDER,
                approval_tier = tier,
                depot_id      = depot_id,
                product_id    = pid,
                conflict_key  = f"reorder_{pid}",
                payload       = {
                    "product_name":        product_name,
                    "reorder_qty":         reorder_qty,
                    "days_until_stockout": round(days_left, 1),
                    "lead_time_days":      lead_time,
                    "current_stock":       float(row.get("current_stock", 0)),
                    "urgency":             urgency,
                    "cashflow_adjusted":   cashflow_negative,
                    "reason": (
                        f"{product_name}: stock out in {days_left:.0f} days "
                        f"(lead time {lead_time:.0f} days). "
                        f"Suggest ordering {reorder_qty} units."
                        + (
                            " [qty reduced 30% — cashflow negative]"
                            if cashflow_negative else ""
                        )
                    ),
                },
                metadata={
                    "days_until_stockout": days_left,
                    "lead_time_days":      lead_time,
                },
            ))

        logger.info(
            f"[{depot_id}] ReorderAgent: {len(decisions)} decisions"
        )
        return decisions
