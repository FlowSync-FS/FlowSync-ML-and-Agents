"""
backend/models/products.py

Products ORM model — global reference table, no RLS.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    ARRAY, Boolean, Column, DateTime,
    Float, Integer, Numeric, String,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from backend.database import Base


class Product(Base):
    __tablename__ = "products"

    id                      = Column(UUID(as_uuid=True), primary_key=True,
                                      default=uuid.uuid4)
    canonical_name          = Column(String(200), nullable=False)
    aliases                 = Column(ARRAY(String), default=list)
    gtin                    = Column(String(14))
    hsn_code                = Column(String(8))
    manufacturer            = Column(String(100))
    product_category        = Column(String(50))
    mrp                     = Column(Numeric(10, 2))
    ptr                     = Column(Numeric(10, 2))
    pts                     = Column(Numeric(10, 2))
    is_cold_chain           = Column(Boolean, default=False)
    storage_temp_min        = Column(Float)
    storage_temp_max        = Column(Float)
    default_shelf_life_days = Column(Integer, default=365)
    schedule_type           = Column(String(5))
    lead_time_days          = Column(Integer, default=7)
    created_at              = Column(DateTime, default=datetime.utcnow)

    # Relationships
    batches = relationship("Batch", back_populates="product")