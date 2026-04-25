"""
backend/schemas/invoices.py

Pydantic v2 request and response schemas for billing + OCR module.
"""

from datetime import date
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


# ── Request schemas ────────────────────────────────────────────────────────────

class InvoiceLineItemConfirm(BaseModel):
    """One corrected line item from the OCR confirm screen."""
    product_id:   UUID
    batch_number: str
    expiry_date:  date
    quantity:     int   = Field(gt=0)
    mrp:          float = Field(gt=0)
    ptr:          float = Field(gt=0)
    gst_percent:  float = Field(ge=0, le=28)


class InvoiceConfirmRequest(BaseModel):
    """
    POST /invoices/{id}/confirm
    Staff confirms or corrects OCR-extracted fields.
    """
    line_items:   list[InvoiceLineItemConfirm]
    confirmed_by: UUID

    @field_validator("line_items")
    @classmethod
    def at_least_one_item(cls, v):
        if not v:
            raise ValueError("At least one line item required")
        return v


# ── Response schemas ───────────────────────────────────────────────────────────

class LineItemOCRResponse(BaseModel):
    """One extracted line item returned by POST /invoices/scan."""
    product_name_raw:  str
    product_id:        Optional[UUID]   = None
    matched_name:      Optional[str]    = None
    batch_number:      Optional[str]    = None
    expiry_date:       Optional[date]   = None
    quantity:          Optional[int]    = None
    mrp:               Optional[float]  = None
    ptr:               Optional[float]  = None
    gst_percent:       Optional[float]  = None
    confidence:        float            = Field(ge=0, le=100)
    needs_correction:  bool             = False


class InvoiceScanResponse(BaseModel):
    """Response from POST /invoices/scan."""
    invoice_id:          UUID
    invoice_number:      Optional[str]  = None
    party_name:          Optional[str]  = None
    invoice_date:        Optional[date] = None
    total_amount:        Optional[float]= None
    line_items:          list[LineItemOCRResponse]
    overall_confidence:  float
    needs_review:        bool
    is_duplicate_flagged: bool

    class Config:
        from_attributes = True


class InvoiceConfirmResponse(BaseModel):
    """Response from POST /invoices/{id}/confirm."""
    invoice_id:               UUID
    stock_movements_created:  int
    batches_created:          int
    status:                   str


class LineItemResponse(BaseModel):
    """One confirmed line item — GET /invoices/{id}/line-items."""
    product_id:    UUID
    canonical_name: str
    batch_number:  str
    expiry_date:   date
    quantity:      int
    mrp:           float
    ptr:           float
    gst_percent:   float

    class Config:
        from_attributes = True


class DuplicateCheckResponse(BaseModel):
    """Response from GET /invoices/{id}/duplicate-check."""
    is_duplicate:        bool
    matching_invoice_id: Optional[UUID]  = None
    similarity_score:    Optional[float] = None
    reason:              Optional[str]   = None