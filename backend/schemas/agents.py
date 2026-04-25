"""Pydantic schemas for agent APIs."""

from datetime import datetime

from pydantic import BaseModel


class AgentRunRequest(BaseModel):
    """Input payload for a queued agent run."""
    depot_id: str
    run_date: str


class AgentRunResponse(BaseModel):
    """Response payload for an agent run enqueue call."""
    status: str
    depot_id: str
    run_date: str
    queued_at: datetime
