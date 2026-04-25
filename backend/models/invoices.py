"""
backend/models/invoices.py

Invoice and InvoiceLineItem ORM models.
Payment, Return, CreditNote are in their own separate files.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean, Column, Date, DateTime,
    Float, Integer, Numeric, String,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from backend.database import Base


class Invoice(Base):
    __tablename__ = "invoices"

    id                   = Column(UUID(as_uuid=True), primary_key=True,
                                   default=uuid.uuid4)
    depot_id             = Column(UUID(as_uuid=True), nullable=False)
    retailer_id          = Column(UUID(as_uuid=True), nullable=True)
    manufacturer_id      = Column(String(100), nullable=True)
    invoice_number       = Column(String(100), nullable=True)
    invoice_date         = Column(Date, nullable=True)
    total_amount         = Column(Numeric(12, 2), nullable=True)
    gst_amount           = Column(Numeric(12, 2), nullable=True)
    status               = Column(String(20), default="PENDING")
    ocr_confidence_score = Column(Float, nullable=True)
    original_image_url   = Column(String(500), nullable=True)
    is_duplicate_flagged = Column(Boolean, default=False)
    created_at           = Column(DateTime, default=datetime.utcnow)

    line_items = relationship(
        "InvoiceLineItem",
        back_populates="invoice",
        cascade="all, delete-orphan",
    )


class InvoiceLineItem(Base):
    __tablename__ = "invoice_line_items"

    id                       = Column(UUID(as_uuid=True), primary_key=True,
                                       default=uuid.uuid4)
    invoice_id               = Column(UUID(as_uuid=True), nullable=False)
    product_id               = Column(UUID(as_uuid=True), nullable=True)
    batch_id                 = Column(UUID(as_uuid=True), nullable=True)
    quantity                 = Column(Integer, nullable=True)
    mrp                      = Column(Numeric(10, 2), nullable=True)
    ptr                      = Column(Numeric(10, 2), nullable=True)
    pts                      = Column(Numeric(10, 2), nullable=True)
    gst_percent              = Column(Float, nullable=True)
    expiry_date_from_invoice = Column(Date, nullable=True)

    invoice = relationship("Invoice", back_populates="line_items")