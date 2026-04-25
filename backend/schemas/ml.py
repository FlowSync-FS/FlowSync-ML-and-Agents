"""
backend/schemas/ml.py

Pydantic v2 schemas for analytics and ML prediction endpoints.
"""

from datetime import date
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, Field


class DemandForecastItem(BaseModel):
    """One product in GET /analytics/demand-forecast/{depot_id}"""
    product_id:          UUID
    canonical_name:      str
    category:            str
    predicted_units_14d: float
    predicted_daily_rate: float
    current_stock:       int
    days_until_stockout: float
    trend:               str   # rising | falling | stable

    class Config:
        from_attributes = True


class SlowMoverItem(BaseModel):
    """One batch in GET /analytics/slow-movers/{depot_id}"""
    batch_id:                     UUID
    product_name:                 str
    expiry_date:                  date
    sales_velocity_weekly:        float
    risk_score:                   float
    recommended_liquidation_date: date
    estimated_loss_if_ignored_inr: float

    class Config:
        from_attributes = True


class InventoryHealthResponse(BaseModel):
    """GET /analytics/inventory-health/{depot_id}"""
    depot_id:               UUID
    health_score:           int   = Field(ge=0, le=100)
    expiry_risk_batches:    int
    anomaly_holds_active:   int
    stockout_risk_products: int
    unreconciled_inr:       float
    fefo_compliance_rate:   float


class CashflowForecastResponse(BaseModel):
    """GET /analytics/cashflow-forecast/{depot_id}"""
    depot_id:                        UUID
    next_30d_projected_collections:  float
    next_30d_expected_payables:      float
    net_cashflow:                    float
    overdue_receivables:             float
    daily_breakdown:                 list[dict]


class RetailerRiskItem(BaseModel):
    """One retailer in GET /analytics/retailer-risk/{depot_id}"""
    retailer_id:       UUID
    name:              str
    credit_risk_score: float
    dso_days:          int
    outstanding_inr:   float
    credit_limit:      float
    risk_band:         str
    last_payment_date: Optional[date] = None

    class Config:
        from_attributes = True