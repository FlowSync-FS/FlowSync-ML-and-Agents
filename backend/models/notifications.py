"""
backend/models/notifications.py

Notification ORM model.
Tracks all sent/queued alerts across channels.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    Column, DateTime, String,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

from backend.database import Base


class Notification(Base):
    __tablename__ = "notifications"

    id                  = Column(UUID(as_uuid=True), primary_key=True,
                                  default=uuid.uuid4)
    depot_id            = Column(UUID(as_uuid=True), nullable=True)
    recipient_user_id   = Column(UUID(as_uuid=True), nullable=True)
    channel             = Column(String(20))   # WHATSAPP | FIREBASE | EMAIL | SMS
    notification_type   = Column(String(50))
    message_template_id = Column(String(50), nullable=True)
    payload             = Column(JSONB, default=dict)
    status              = Column(String(20), default="QUEUED")
    sent_at             = Column(DateTime, default=datetime.utcnow)