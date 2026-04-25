"""Pydantic schemas for compliance APIs."""

from pydantic import BaseModel


class ComplianceStatusResponse(BaseModel):
    """Compliance status summary response."""
    depot_id: str
    status: str
    open_issues: int
