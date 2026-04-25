"""Pydantic schemas for retailer APIs."""

from pydantic import BaseModel


class RetailerSummaryResponse(BaseModel):
    """Output payload for retailer list endpoints."""
    retailer_id: str
    retailer_name: str
    credit_days: int
