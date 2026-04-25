"""
backend/models/depots.py

Depots ORM model.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean, Column, DateTime, Integer,
    String, Text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from backend.database import Base


class Depot(Base):
    __tablename__ = "depots"

    id                 = Column(UUID(as_uuid=True), primary_key=True,
                                 default=uuid.uuid4)
    client_id          = Column(UUID(as_uuid=True), nullable=False)
    name               = Column(String(200), nullable=False)
    gstin              = Column(String(15), unique=True)
    address            = Column(Text)
    region             = Column(String(50))
    license_number     = Column(String(50))
    max_capacity_units = Column(Integer, default=10000)
    is_active          = Column(Boolean, default=True)
    created_at         = Column(DateTime, default=datetime.utcnow)

    # Relationships
    users    = relationship("User",    back_populates="depot")
    batches  = relationship("Batch",   back_populates="depot")
    retailers = relationship("Retailer", back_populates="depot")