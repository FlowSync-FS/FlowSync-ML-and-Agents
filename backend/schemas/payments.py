"""Pydantic schemas for payment APIs."""

from pydantic import BaseModel, Field


class PaymentCreateRequest(BaseModel):
    """Input payload for payment creation."""
    invoice_id: str
    amount: float = Field(gt=0)


class PaymentCreateResponse(BaseModel):
    """Output payload for payment creation."""
    payment_id: str
    status: str
