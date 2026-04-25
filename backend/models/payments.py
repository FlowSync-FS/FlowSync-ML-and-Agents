"""
backend/models/payments.py

Payment ORM model — standalone file.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    Column, DateTime, Float,
    Numeric, String,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from backend.database import Base


class Payment(Base):
    __tablename__ = "payments"

    id                    = Column(UUID(as_uuid=True), primary_key=True,
                                    default=uuid.uuid4)
    depot_id              = Column(UUID(as_uuid=True), nullable=False)
    invoice_id            = Column(UUID(as_uuid=True), nullable=True)
    retailer_id           = Column(UUID(as_uuid=True), nullable=True)
    amount_paid           = Column(Numeric(12, 2), nullable=False)
    payment_mode          = Column(String(10))   # CASH|UPI|CHEQUE|NEFT|RTGS
    collected_by          = Column(UUID(as_uuid=True), nullable=True)
    gps_lat               = Column(Float, nullable=True)
    gps_lng               = Column(Float, nullable=True)
    photo_url             = Column(String(500), nullable=True)
    reconciliation_status = Column(String(20), default="UNLINKED")
    collected_at          = Column(DateTime, default=datetime.utcnow)