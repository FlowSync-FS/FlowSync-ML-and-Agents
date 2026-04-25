"""
backend/models/returns.py

Return and CreditNote ORM models — standalone file.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean, Column, DateTime,
    Integer, Numeric, String, Text,
)
from sqlalchemy.dialects.postgresql import UUID

from backend.database import Base


class Return(Base):
    __tablename__ = "returns"

    id                     = Column(UUID(as_uuid=True), primary_key=True,
                                     default=uuid.uuid4)
    depot_id               = Column(UUID(as_uuid=True), nullable=False)
    original_invoice_id    = Column(UUID(as_uuid=True), nullable=True)
    batch_id               = Column(UUID(as_uuid=True), nullable=True)
    product_id             = Column(UUID(as_uuid=True), nullable=True)
    quantity_returned      = Column(Integer)
    return_reason          = Column(Text)
    photo_proof_url        = Column(String(500))
    is_fake_return_flagged = Column(Boolean, default=False)
    credit_note_id         = Column(UUID(as_uuid=True), nullable=True)
    created_at             = Column(DateTime, default=datetime.utcnow)


class CreditNote(Base):
    __tablename__ = "credit_notes"

    id         = Column(UUID(as_uuid=True), primary_key=True,
                         default=uuid.uuid4)
    depot_id   = Column(UUID(as_uuid=True), nullable=False)
    return_id  = Column(UUID(as_uuid=True), nullable=True)
    invoice_id = Column(UUID(as_uuid=True), nullable=True)
    amount     = Column(Numeric(12, 2))
    status     = Column(String(20), default="ISSUED")
    issued_at  = Column(DateTime, default=datetime.utcnow)