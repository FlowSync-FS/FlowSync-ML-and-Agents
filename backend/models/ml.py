"""
backend/models/ml.py

ORM models for all ML prediction and agent tables.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean, Column, Date, DateTime,
    Float, Integer, String, Text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

from backend.database import Base


class DemandPrediction(Base):
    __tablename__ = "demand_predictions"

    id                   = Column(UUID(as_uuid=True), primary_key=True,
                                   default=uuid.uuid4)
    depot_id             = Column(UUID(as_uuid=True), nullable=False)
    product_id           = Column(UUID(as_uuid=True), nullable=False)
    run_date             = Column(Date, nullable=False)
    predicted_units_14d  = Column(Float)
    predicted_daily_rate = Column(Float)
    demand_trend_slope   = Column(Float)
    created_at           = Column(DateTime, default=datetime.utcnow)


class ExpiryPrediction(Base):
    __tablename__ = "expiry_predictions"

    id                           = Column(UUID(as_uuid=True), primary_key=True,
                                           default=uuid.uuid4)
    depot_id                     = Column(UUID(as_uuid=True), nullable=False)
    batch_id                     = Column(UUID(as_uuid=True), nullable=False)
    run_date                     = Column(Date, nullable=False)
    expiry_risk_score            = Column(Float)
    recommended_liquidation_date = Column(Date)
    method                       = Column(String(10), default="formula")
    created_at                   = Column(DateTime, default=datetime.utcnow)


class FefoRanking(Base):
    __tablename__ = "fefo_rankings"

    id                = Column(UUID(as_uuid=True), primary_key=True,
                                default=uuid.uuid4)
    depot_id          = Column(UUID(as_uuid=True), nullable=False)
    batch_id          = Column(UUID(as_uuid=True), nullable=False)
    run_date          = Column(Date, nullable=False)
    priority_rank     = Column(Integer)
    priority_score    = Column(Float)
    ml_override       = Column(Boolean, default=False)
    expiry_risk_score = Column(Float)
    days_till_expiry  = Column(Integer)
    created_at        = Column(DateTime, default=datetime.utcnow)


class AgentAction(Base):
    __tablename__ = "agent_actions"

    id            = Column(UUID(as_uuid=True), primary_key=True,
                            default=uuid.uuid4)
    depot_id      = Column(UUID(as_uuid=True), nullable=False)
    agent         = Column(String(50))
    action_type   = Column(String(50))
    approval_tier = Column(String(10))
    batch_id      = Column(UUID(as_uuid=True), nullable=True)
    product_id    = Column(UUID(as_uuid=True), nullable=True)
    conflict_key  = Column(String(200))
    payload        = Column(JSONB, default=dict)
    extra_metadata = Column(JSONB, default=dict, name="metadata")
    outcome        = Column(String(20), default="PENDING_APPROVAL")
    decided_by    = Column(UUID(as_uuid=True), nullable=True)
    decided_at    = Column(DateTime, nullable=True)
    created_at    = Column(DateTime, default=datetime.utcnow)


class ModelRegistry(Base):
    __tablename__ = "model_registry"

    id          = Column(UUID(as_uuid=True), primary_key=True,
                          default=uuid.uuid4)
    model_name  = Column(String(100), nullable=False)
    version     = Column(String(30),  nullable=False)
    s3_key      = Column(String(300), nullable=False)
    trained_at  = Column(DateTime, nullable=False)
    is_active       = Column(Boolean, default=True)
    extra_metadata  = Column(JSONB, default=dict, name="metadata")


class PipelineRunLog(Base):
    __tablename__ = "pipeline_run_logs"

    id          = Column(UUID(as_uuid=True), primary_key=True,
                          default=uuid.uuid4)
    depot_id    = Column(UUID(as_uuid=True), nullable=False)
    run_date    = Column(Date, nullable=False)
    status      = Column(String(10))
    stages      = Column(JSONB, default=dict)
    started_at  = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)


class DriftLog(Base):
    __tablename__ = "drift_logs"

    id                = Column(UUID(as_uuid=True), primary_key=True,
                                default=uuid.uuid4)
    model_name        = Column(String(100))
    psi_score         = Column(Float)
    checked_at        = Column(DateTime, default=datetime.utcnow)
    retraining_needed = Column(Boolean, default=False)


class SystemAlert(Base):
    __tablename__ = "system_alerts"

    id          = Column(UUID(as_uuid=True), primary_key=True,
                          default=uuid.uuid4)
    depot_id    = Column(UUID(as_uuid=True), nullable=True)
    alert_type  = Column(String(50))
    message     = Column(Text)
    resolved    = Column(Boolean, default=False)
    created_at  = Column(DateTime, default=datetime.utcnow)