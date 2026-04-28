"""
ml/agents/dispatch_agent.py

DispatchAgent — in-memory BaseAgent.
Consumes state["fefo_rankings"] (list of dicts from infer_fefo.py Stage 3).

Excludes any batch in state["held_batches"] (set[str] of ANOMALY_HOLD batch_ids).
Emits DISPATCH_PLAN (AUTO) for every non-held batch that has positive demand.

conflict_key = dispatch_{batch_id}
"""

import logging
from typing import List

from ml.agents.base import (
    AgentDecision,
    ActionType,
    ApprovalTier,
    BaseAgent,
)

logger = logging.getLogger("flowsync.agents.dispatch")


class DispatchAgent(BaseAgent):
    """
    Converts FEFO-ranked batch list into DISPATCH_PLAN decisions.

    Expects state dict keys:
        depot_id      (str)
        fefo_rankings (list[dict]) — each dict: batch_id, product_id,
                      priority_rank, days_till_expiry, ml_override,
                      units_available, predicted_units_14d,
                      product_name, expiry_date
        held_batches  (list[str] | set[str]) — batch_ids under ANOMALY_HOLD
    """

    name = "dispatch"

    def observe(self, state: dict) -> dict:
        """
        Extract FEFO rankings, demand data, and held batch set from shared state.

        Enriches each fefo ranking row with predicted_units_14d from
        state["demand_forecast"] when that column is absent in the ranking.

        Args:
            state: Shared orchestrator state dict.

        Returns:
            Dict with depot_id, fefo_rankings (enriched), and held_batches set.
        """
        demand_by_pid: dict[str, float] = {}
        for d in state.get("demand_forecast", []):
            pid = str(d.get("product_id", ""))
            if pid:
                demand_by_pid[pid] = float(d.get("predicted_units_14d", 0))

        enriched = []
        for row in state.get("fefo_rankings", []):
            r   = dict(row)
            pid = str(r.get("product_id", ""))
            if r.get("predicted_units_14d") is None:
                r["predicted_units_14d"] = demand_by_pid.get(pid, 0.0)
            enriched.append(r)

        return {
            "depot_id":      state.get("depot_id", ""),
            "fefo_rankings": enriched,
            "held_batches":  set(state.get("held_batches", [])),
        }

    def act(self, observations: dict) -> List[AgentDecision]:
        """
        Emit DISPATCH_PLAN for each eligible batch.

        Args:
            observations: Output of observe().

        Returns:
            List of AgentDecision with action=DISPATCH_PLAN (AUTO tier).
        """
        depot_id     = observations["depot_id"]
        held_batches = observations["held_batches"]
        decisions: List[AgentDecision] = []

        for row in observations["fefo_rankings"]:
            bid = str(row.get("batch_id", ""))

            if bid in held_batches:
                logger.debug(
                    f"[{depot_id}] dispatch skipped {bid} — ANOMALY_HOLD active"
                )
                continue

            units_avail  = float(row.get("units_available", 0))
            demand_14d   = float(row.get("predicted_units_14d", 0))
            units_to_go  = min(units_avail, demand_14d)

            if units_to_go <= 0:
                continue

            product_name  = str(row.get("product_name", "Unknown"))
            days_expiry   = row.get("days_till_expiry", 0)
            priority_rank = int(row.get("priority_rank", 0))

            decisions.append(AgentDecision(
                agent         = "DispatchAgent",
                action        = ActionType.DISPATCH_PLAN,
                approval_tier = ApprovalTier.AUTO,
                depot_id      = depot_id,
                batch_id      = bid,
                product_id    = str(row.get("product_id", "")),
                conflict_key  = f"dispatch_{bid}",
                payload       = {
                    "units_to_dispatch":   round(units_to_go, 0),
                    "units_available":     round(units_avail, 0),
                    "predicted_units_14d": demand_14d,
                    "days_till_expiry":    days_expiry,
                    "expiry_date":         str(row.get("expiry_date", "")),
                    "priority_rank":       priority_rank,
                    "ml_override":         bool(row.get("ml_override", False)),
                    "product_name":        product_name,
                    "reason": (
                        f"{product_name}: dispatch {units_to_go:.0f} units "
                        f"(expires in {days_expiry}d, rank #{priority_rank})"
                    ),
                },
                metadata={"priority_rank": priority_rank},
            ))

        logger.info(
            f"[{depot_id}] DispatchAgent: {len(decisions)} DISPATCH_PLAN decisions"
        )
        return decisions
