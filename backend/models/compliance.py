"""
backend/models/compliance.py

Recall and AuditTrail ORM models.
audit_trail is INSERT-only — enforced by DB trigger.
Never attempt UPDATE or DELETE on audit_trail.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean, Column, Date, DateTime,
    Integer, String, Text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

from backend.database import Base


class Recall(Base):
    __tablename__ = "recalls"

    id                     = Column(UUID(as_uuid=True), primary_key=True,
                                     default=uuid.uuid4)
    depot_id               = Column(UUID(as_uuid=True), nullable=False)
    batch_id               = Column(UUID(as_uuid=True), nullable=False)
    product_id             = Column(UUID(as_uuid=True), nullable=False)
    recall_issued_by       = Column(String(100))
    cdsco_reference_number = Column(String(50))
    recall_date            = Column(Date)
    affected_quantity      = Column(Integer)
    status                 = Column(String(20), default="INITIATED")
    completion_report_url  = Column(String(500), nullable=True)
    created_at             = Column(DateTime, default=datetime.utcnow)


class AuditTrail(Base):
    """
    INSERT-only table. DB trigger raises exception on UPDATE or DELETE.
    Never call db.delete() or UPDATE on this model.
    """
    __tablename__ = "audit_trail"

    id           = Column(UUID(as_uuid=True), primary_key=True,
                           default=uuid.uuid4)
    event_type   = Column(String(50),  nullable=False)
    entity_table = Column(String(50),  nullable=False)
    entity_id    = Column(UUID(as_uuid=True), nullable=True)
    performed_by = Column(UUID(as_uuid=True), nullable=True)
    old_value    = Column(JSONB, nullable=True)
    new_value    = Column(JSONB, nullable=True)
    ip_address   = Column(String(45), nullable=True)
    created_at   = Column(DateTime, default=datetime.utcnow)


class DiscountScheme(Base):
    __tablename__ = "discount_schemes"

    id                = Column(UUID(as_uuid=True), primary_key=True,
                                default=uuid.uuid4)
    depot_id          = Column(UUID(as_uuid=True), nullable=False)
    manufacturer_name = Column(String(100))
    scheme_name       = Column(String(100))
    scheme_type       = Column(String(50))
    terms             = Column(JSONB, default=dict)
    valid_from        = Column(Date, nullable=True)
    valid_to          = Column(Date, nullable=True)