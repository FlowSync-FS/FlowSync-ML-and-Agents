"""
ml/agents/coordinator.py

Multi-agent coordinator: runs all agents, resolves conflicts, and returns a
priority-sorted action list.

Conflict rule (spec ml-rules.md):
  When SUGGEST_LIQUIDATION is active for a product, SUGGEST_REORDER for the
  same product is suppressed — never reorder what you're liquidating.

Priority order (never reorder, per spec):
  1. ANOMALY_HOLD
  2. SUGGEST_LIQUIDATION
  3. FLAG_PRIORITY_DISPATCH
  4. SUGGEST_REORDER
  5. DISPATCH_PLAN
  6. ANOMALY_ALERT
"""

import logging
from typing import Any

from ml.agents.anomaly_agent import AnomalyAgent
from ml.agents.base import (
    ActionType,
    AgentDecision,
    BaseAgent,
    COORDINATOR_PRIORITY,
    SUGGEST_LIQUIDATION,
    SUGGEST_REORDER,
)
from ml.agents.dispatch_agent import DispatchAgent
from ml.agents.expiry_agent import ExpiryAgent
from ml.agents.reorder_agent import ReorderAgent

logger = logging.getLogger("flowsync.agents.coordinator")


class AgentCoordinator:
    """
    Orchestrates all FlowSync agents against shared system state.
    Agents run in sequence; conflicting actions are resolved before returning.
    """

    def __init__(self, config: dict = None) -> None:
        """
        Initialise coordinator with per-agent config sub-dicts.

        Args:
            config: Optional dict with keys anomaly, expiry, reorder, dispatch.
        """
        cfg = config or {}
        self.agents: list[BaseAgent] = [
            AnomalyAgent(cfg.get("anomaly", {})),
            ExpiryAgent(cfg.get("expiry", {})),
            ReorderAgent(cfg.get("reorder", {})),
            DispatchAgent(cfg.get("dispatch", {})),
        ]

    def run(self, state: dict) -> list[dict[str, Any]]:
        """
        Run all agents and return conflict-resolved, priority-sorted actions.

        Args:
            state: Combined state dict — keys consumed by each individual agent.
                   See agent docstrings for their expected keys.

        Returns:
            List of action dicts sorted by priority ascending (1 = most urgent).
        """
        all_actions: list[AgentDecision] = []
        for agent in self.agents:
            actions = agent.run(state)
            all_actions.extend(actions)
            logger.info(f"[coordinator] {agent.name}: {len(actions)} actions")

        resolved = self._resolve_conflicts(all_actions)
        resolved.sort(key=lambda a: COORDINATOR_PRIORITY.get(a.action, 99))

        logger.info(
            f"[coordinator] total: {len(resolved)} actions after conflict resolution "
            f"(suppressed: {len(all_actions) - len(resolved)})"
        )
        return [a.to_dict() for a in resolved]

    def resolve_and_commit(self, state: dict) -> list[dict[str, Any]]:
        """
        Spec-mandated entry point called by orchestrator at pipeline step 6.
        Alias for run() — resolves conflicts and commits the final action list.

        Args:
            state: Combined system state dict.

        Returns:
            List of resolved action dicts.
        """
        return self.run(state)

    def run_agent(self, agent_name: str, state: dict) -> list[dict[str, Any]]:
        """
        Run a single named agent without conflict resolution.

        Args:
            agent_name: Name attribute of the target agent.
            state:      System state dict.

        Returns:
            List of action dicts from that agent only.

        Raises:
            ValueError: If agent_name not found.
        """
        for agent in self.agents:
            if agent.name == agent_name:
                return [a.to_dict() for a in agent.run(state)]
        raise ValueError(
            f"Unknown agent: '{agent_name}'. "
            f"Available: {[a.name for a in self.agents]}"
        )

    # ── Conflict resolution ──────────────────────────────────────────

    @staticmethod
    def _resolve_conflicts(actions: list[AgentDecision]) -> list[AgentDecision]:
        """
        Apply spec conflict rules to the raw action list.

        Rule: suppress SUGGEST_REORDER for any product that already has an
        active SUGGEST_LIQUIDATION — never reorder what you're liquidating.

        Args:
            actions: Raw list of AgentDecision objects from all agents.

        Returns:
            Filtered list with conflicting actions removed.
        """
        liquidation_products: set[str] = set()
        for action in actions:
            if action.action == ActionType.SUGGEST_LIQUIDATION:
                product_id = action.payload.get("product_id") or action.product_id or ""
                if product_id:
                    liquidation_products.add(str(product_id))

        if not liquidation_products:
            return actions

        resolved = []
        suppressed = 0
        for action in actions:
            if action.action == ActionType.SUGGEST_REORDER:
                product_id = action.product_id or action.payload.get("product_id", "")
                if str(product_id) in liquidation_products:
                    logger.info(
                        f"[coordinator] suppressed SUGGEST_REORDER for {product_id} "
                        f"(SUGGEST_LIQUIDATION active)"
                    )
                    suppressed += 1
                    continue
            resolved.append(action)

        return resolved
