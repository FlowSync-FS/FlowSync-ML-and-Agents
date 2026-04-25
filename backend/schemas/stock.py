"""
backend/schemas/stock.py

Pydantic v2 schemas for inventory endpoints.
"""

from datetime import date, datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, Field


class BatchInStock(BaseModel):
    """One batch entry within a stock response."""
    batch_id:          UUID
    batch_number:      str
    expiry_date:       date
    quantity_remaining: int
    expiry_risk_score: Optional[float] = None
    fefo_rank:         Optional[int]   = None
    status:            str             = "active"

    class Config:
        from_attributes = True


class StockResponse(BaseModel):
    """GET /inventory/stock/{depot_id} — one product entry."""
    product_id:     UUID
    canonical_name: str
    category:       str
    total_stock:    int
    batches:        list[BatchInStock]

    class Config:
        from_attributes = True


class StockMovementResponse(BaseModel):
    """One movement in batch timeline."""
    movement_id:   UUID
    movement_type: str
    quantity:      int
    performed_by:  Optional[UUID] = None
    created_at:    datetime

    class Config:
        from_attributes = True


class BatchDetailResponse(BaseModel):
    """GET /inventory/batch/{batch_id} — full batch history."""
    batch_id:          UUID
    batch_number:      str
    product_id:        UUID
    canonical_name:    str
    expiry_date:       date
    quantity_received: int
    quantity_remaining: int
    quantity_sold:     int
    status:            str
    movements:         list[StockMovementResponse]

    class Config:
        from_attributes = True


class StockInRequest(BaseModel):
    """POST /inventory/stock-in — manual stock-in fallback."""
    invoice_id:    Optional[UUID] = None
    batch_number:  str
    product_id:    UUID
    quantity:      int            = Field(gt=0)
    expiry_date:   date
    performed_by:  UUID


class StockInResponse(BaseModel):
    movement_id:         UUID
    batch_id:            UUID
    current_stock_after: int