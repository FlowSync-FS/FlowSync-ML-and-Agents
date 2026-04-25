"""
backend/models/stock.py

StockMovement ORM model.
Retailer is in its own retailers.py file.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    Column, DateTime, Integer, String, Text,
)
from sqlalchemy.dialects.postgresql import UUID

from backend.database import Base


class StockMovement(Base):
    __tablename__ = "stock_movements"

    id            = Column(UUID(as_uuid=True), primary_key=True,
                            default=uuid.uuid4)
    depot_id      = Column(UUID(as_uuid=True), nullable=False)
    product_id    = Column(UUID(as_uuid=True), nullable=False)
    batch_id      = Column(UUID(as_uuid=True), nullable=False)
    movement_type = Column(String(10), nullable=False)
    quantity      = Column(Integer, nullable=False)
    performed_by  = Column(UUID(as_uuid=True), nullable=True)
    reference_id  = Column(UUID(as_uuid=True), nullable=True)
    notes         = Column(Text, nullable=True)
    created_at    = Column(DateTime, default=datetime.utcnow)