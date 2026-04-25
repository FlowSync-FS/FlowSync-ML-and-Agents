"""
backend/models/retailers.py

Retailer ORM model — standalone file.
The depot's customers. Central to reconciliation,
DSO tracking, and credit risk scoring.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    Column, DateTime, Float,
    Integer, Numeric, String, Text,
)
from sqlalchemy.dialects.postgresql import UUID

from backend.database import Base


class Retailer(Base):
    __tablename__ = "retailers"

    id                  = Column(UUID(as_uuid=True), primary_key=True,
                                  default=uuid.uuid4)
    depot_id            = Column(UUID(as_uuid=True), nullable=False)
    name                = Column(String(200), nullable=False)
    gstin               = Column(String(15), nullable=True)
    address             = Column(Text, nullable=True)
    phone               = Column(String(15), nullable=True)
    credit_limit        = Column(Numeric(12, 2), default=50000)
    current_outstanding = Column(Numeric(12, 2), default=0)
    dso_days            = Column(Integer, default=0)
    credit_risk_score   = Column(Float, default=5.0)
    created_at          = Column(DateTime, default=datetime.utcnow)