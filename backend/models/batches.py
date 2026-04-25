"""
backend/models/batches.py

Batches ORM model — batch-level tracking.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    Column, Date, DateTime, ForeignKey,
    Integer, String,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from backend.database import Base


class Batch(Base):
    __tablename__ = "batches"

    id                 = Column(UUID(as_uuid=True), primary_key=True,
                                 default=uuid.uuid4)
    depot_id           = Column(UUID(as_uuid=True),
                                 ForeignKey("depots.id"))
    product_id         = Column(UUID(as_uuid=True),
                                 ForeignKey("products.id"))
    batch_number       = Column(String(50))
    expiry_date        = Column(Date, nullable=False)
    manufacturer       = Column(String(100))
    quantity_received  = Column(Integer, nullable=False)
    quantity_remaining = Column(Integer)
    invoice_id         = Column(UUID(as_uuid=True), nullable=True)
    created_at         = Column(DateTime, default=datetime.utcnow)

    # Relationships
    depot   = relationship("Depot",   back_populates="batches")
    product = relationship("Product", back_populates="batches")