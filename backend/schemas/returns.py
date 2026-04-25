"""Pydantic schemas for returns APIs."""

from pydantic import BaseModel, Field


class ReturnCreateRequest(BaseModel):
    """Input payload for return creation."""
    invoice_id: str
    reason: str
    amount: float = Field(ge=0)


class ReturnCreateResponse(BaseModel):
    """Output payload for return creation."""
    return_id: str
    status: str
