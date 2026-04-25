"""
ml/agents/base.py

AgentDecision dataclass, ActionType enum, ApprovalTier enum.
COORDINATOR_PRIORITY dict — never reorder this.

Every agent imports from here.
Never define these in any other file.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class ActionType(Enum):
    # ExpiryPreventionAgent
    SUGGEST_LIQUIDATION    = "SUGGEST_LIQUIDATION"
    FLAG_PRIORITY_DISPATCH = "FLAG_PRIORITY_DISPATCH"
    # ReorderAgent
    SUGGEST_REORDER        = "SUGGEST_REORDER"
    # DispatchAgent
    DISPATCH_PLAN          = "DISPATCH_PLAN"
    # AnomalyAgent
    ANOMALY_HOLD           = "ANOMALY_HOLD"
    ANOMALY_ALERT          = "ANOMALY_ALERT"


class ApprovalTier(Enum):
    AUTO    = "AUTO"     # executes without human, no notification
    NOTIFY  = "NOTIFY"   # WhatsApp fires immediately, operation proceeds
    APPROVE = "APPROVE"  # operation BLOCKED until manager acts or 24h expires


@dataclass
class AgentDecision:
    """
    Single decision produced by one agent.
    Passed to CoordinatorAgent for deduplication + conflict resolution.
    """
    agent:          str
    action:         ActionType
    approval_tier:  ApprovalTier
    depot_id:       str
    payload:        dict                 = field(default_factory=dict)
    batch_id:       Optional[str]        = None
    product_id:     Optional[str]        = None
    conflict_key:   Optional[str]        = None
    metadata:       Optional[dict]       = None

    def __post_init__(self):
        """Auto-generate conflict_key if not provided."""
        if self.conflict_key is None:
            if self.batch_id:
                self.conflict_key = (
                    f"{self.action.value}_{self.batch_id}"
                )
            elif self.product_id:
                self.conflict_key = (
                    f"{self.action.value}_{self.product_id}"
                )
            else:
                self.conflict_key = (
                    f"{self.action.value}_{self.depot_id}"
                )


# ── Coordinator priority order ─────────────────────────────────────────────────
# Lower number = higher priority.
# This is a system invariant — never reorder without full team discussion.
# Changing this order changes system behaviour for every depot.

COORDINATOR_PRIORITY: dict[ActionType, int] = {
    ActionType.ANOMALY_HOLD:           1,
    ActionType.SUGGEST_LIQUIDATION:    2,
    ActionType.FLAG_PRIORITY_DISPATCH: 3,
    ActionType.SUGGEST_REORDER:        4,
    ActionType.DISPATCH_PLAN:          5,
    ActionType.ANOMALY_ALERT:          6,
}

# ── Convenience string aliases (used by agents for readability) ───────────────
ANOMALY_HOLD           = ActionType.ANOMALY_HOLD.value
SUGGEST_LIQUIDATION    = ActionType.SUGGEST_LIQUIDATION.value
FLAG_PRIORITY_DISPATCH = ActionType.FLAG_PRIORITY_DISPATCH.value
SUGGEST_REORDER        = ActionType.SUGGEST_REORDER.value
DISPATCH_PLAN          = ActionType.DISPATCH_PLAN.value
ANOMALY_ALERT          = ActionType.ANOMALY_ALERT.value


def _decision_to_dict(d: "AgentDecision") -> dict:
    return {
        "agent":         d.agent,
        "action_type":   d.action.value,
        "approval_tier": d.approval_tier.value,
        "depot_id":      d.depot_id,
        "batch_id":      d.batch_id,
        "product_id":    d.product_id,
        "conflict_key":  d.conflict_key,
        "payload":       d.payload,
    }


# Patch to_dict onto AgentDecision after definition
AgentDecision.to_dict = _decision_to_dict


# ── BaseAgent — in-memory synchronous interface ───────────────────────────────
import logging
from abc import ABC, abstractmethod


class BaseAgent(ABC):
    """Abstract base for all in-memory (non-DB) agents used by the orchestrator."""

    name: str = "base"

    def __init__(self, config: dict = None) -> None:
        self.config = config or {}
        self.logger = logging.getLogger(f"flowsync.agents.{self.name}")

    @abstractmethod
    def observe(self, state: dict) -> dict:
        """Extract agent-relevant signals from shared state."""
        ...

    @abstractmethod
    def act(self, observations: dict) -> list:
        """Produce AgentDecision objects from observations."""
        ...

    def run(self, state: dict) -> list:
        """observe -> act with error isolation."""
        try:
            obs     = self.observe(state)
            actions = self.act(obs)
            self.logger.info(f"{self.name}: {len(actions)} decisions")
            return actions
        except Exception as exc:
            self.logger.error(f"{self.name} failed: {exc}", exc_info=True)
            return []


# Backwards-compat alias
AgentAction = AgentDecision