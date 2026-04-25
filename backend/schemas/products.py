"""Pydantic schemas for product APIs."""

from pydantic import BaseModel


class ProductSummaryResponse(BaseModel):
    """Output payload for product list endpoints."""
    product_id: str
    canonical_name: str
    product_category: str
