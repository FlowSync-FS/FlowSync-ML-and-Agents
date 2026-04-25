"""
backend/models/users.py

Users ORM model.
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from backend.database import Base


class User(Base):
    __tablename__ = "users"

    id            = Column(UUID(as_uuid=True), primary_key=True,
                            default=uuid.uuid4)
    depot_id      = Column(UUID(as_uuid=True), ForeignKey("depots.id",
                            ondelete="CASCADE"), nullable=True)
    client_id     = Column(UUID(as_uuid=True), nullable=False)
    name          = Column(String(100))
    phone         = Column(String(15))
    email         = Column(String(100))
    role          = Column(String(20), nullable=False)
    password_hash = Column(String(200))
    is_active     = Column(Boolean, default=True)
    created_at    = Column(DateTime, default=datetime.utcnow)

    # Relationships
    depot = relationship("Depot", back_populates="users")