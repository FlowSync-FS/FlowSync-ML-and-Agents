"""
backend/models/temperature.py

TemperatureLog and IoTDevice ORM models.
temperature_logs is INSERT-only — enforced by DB trigger.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean, Column, DateTime, Float,
    String,
)
from sqlalchemy.dialects.postgresql import UUID

from backend.database import Base


class TemperatureLog(Base):
    __tablename__ = "temperature_logs"

    id                      = Column(UUID(as_uuid=True), primary_key=True,
                                      default=uuid.uuid4)
    depot_id                = Column(UUID(as_uuid=True), nullable=False)
    batch_id                = Column(UUID(as_uuid=True), nullable=True)
    reading_value           = Column(Float, nullable=False)
    reading_type            = Column(String(10), nullable=False)  # PHOTO | SENSOR
    photo_url               = Column(String(500), nullable=True)
    is_excursion            = Column(Boolean, default=False)
    excursion_threshold_min = Column(Float, nullable=True)
    excursion_threshold_max = Column(Float, nullable=True)
    gps_lat                 = Column(Float, nullable=True)
    gps_lng                 = Column(Float, nullable=True)
    logged_by               = Column(UUID(as_uuid=True), nullable=True)
    device_id               = Column(String(50), nullable=True)
    logged_at               = Column(DateTime, default=datetime.utcnow)


class IoTDevice(Base):
    __tablename__ = "iot_devices"

    id               = Column(UUID(as_uuid=True), primary_key=True,
                               default=uuid.uuid4)
    depot_id         = Column(UUID(as_uuid=True), nullable=False)
    device_id        = Column(String(50), unique=True, nullable=False)
    fridge_label     = Column(String(50))
    is_active        = Column(Boolean, default=True)
    last_seen_at     = Column(DateTime, nullable=True)
    firmware_version = Column(String(20), nullable=True)
    created_at       = Column(DateTime, default=datetime.utcnow)