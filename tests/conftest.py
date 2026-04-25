"""
tests/conftest.py

Shared pytest fixtures for all test modules.
Uses an in-memory SQLite database for speed.
Real schema applied via SQLAlchemy ORM.
No external services (S3, WhatsApp, Redis) needed for unit tests.
"""

import asyncio
import json
import uuid
from datetime import date, datetime
from typing import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import create_engine, event, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# ── In-memory SQLite engine for tests ─────────────────────────────────────────
TEST_DB_URL       = "sqlite+aiosqlite:///:memory:"
TEST_DB_URL_SYNC  = "sqlite:///:memory:"

engine_test = create_async_engine(
    TEST_DB_URL,
    connect_args = {"check_same_thread": False},
    poolclass    = StaticPool,
)

AsyncTestSession = sessionmaker(
    bind             = engine_test,
    class_           = AsyncSession,
    expire_on_commit = False,
    autoflush        = False,
)


@pytest.fixture(scope="session")
def event_loop():
    """Single event loop for entire test session."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture
async def db() -> AsyncGenerator[AsyncSession, None]:
    """
    Async DB session for each test.
    Creates all tables, yields session, rolls back after test.
    """
    from backend.database import Base
    import backend.models  # noqa: F401 - registers all ORM models

    async with engine_test.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with AsyncTestSession() as session:
        yield session
        await session.rollback()

    async with engine_test.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


# ── Seed fixtures ──────────────────────────────────────────────────────────────

@pytest.fixture
def sample_depot_id() -> str:
    return str(uuid.uuid4())


@pytest.fixture
def sample_product_id() -> str:
    return str(uuid.uuid4())


@pytest.fixture
def sample_batch_id() -> str:
    return str(uuid.uuid4())


@pytest.fixture
def sample_depot(sample_depot_id) -> dict:
    return {
        "id":                  sample_depot_id,
        "client_id":           str(uuid.uuid4()),
        "name":                "Test Depot Bhubaneswar",
        "gstin":               "21ABCDE1234F1Z5",
        "region":              "Odisha",
        "max_capacity_units":  10000,
        "is_active":           True,
    }


@pytest.fixture
def sample_product(sample_product_id) -> dict:
    return {
        "id":                      sample_product_id,
        "canonical_name":          "Paracetamol 500mg",
        "aliases":                 ["Crocin 500", "Dolo 500"],
        "product_category":        "pain_relief",
        "is_cold_chain":           False,
        "default_shelf_life_days": 1095,
        "schedule_type":           "OTC",
        "mrp":                     15.0,
        "ptr":                     12.0,
        "pts":                     10.0,
        "lead_time_days":          7,
    }


@pytest.fixture
def sample_batch(sample_batch_id, sample_product_id, sample_depot_id) -> dict:
    return {
        "id":                sample_batch_id,
        "depot_id":          sample_depot_id,
        "product_id":        sample_product_id,
        "batch_number":      "BATCH001",
        "expiry_date":       date(2027, 6, 30),
        "quantity_received": 100,
        "created_at":        datetime.utcnow(),
    }


@pytest.fixture
def mock_model_store():
    """Mock ModelStore so tests never hit S3."""
    with patch("ml.registry.model_store.ModelStore") as mock:
        store          = MagicMock()
        store.load     = MagicMock(return_value=MagicMock())
        store.save     = MagicMock(return_value="models/test/20251001.pkl")
        mock.return_value = store
        yield store


@pytest.fixture
def mock_whatsapp():
    """Prevent real WhatsApp sends during tests."""
    with patch(
        "backend.services.notification_service.send_whatsapp",
        new_callable=AsyncMock,
        return_value=True,
    ) as mock:
        yield mock


@pytest.fixture
def compliance_config() -> dict:
    """Config values matching seed_compliance_config.py defaults."""
    return {
        "fefo_override_threshold":       0.6,
        "expiry_critical_threshold":     0.85,
        "expiry_warning_threshold":      0.60,
        "anomaly_hold_threshold":        2.5,
        "anomaly_alert_threshold":       2.0,
        "default_lead_time_days":        7,
        "cashflow_reorder_reduction":    0.3,
        "exact_match_tolerance":         0.01,
        "consolidated_match_tolerance":  0.02,
        "consolidation_invoice_limit":   5,
        "festival_dates": [
            "2025-10-20", "2025-10-23",
            "2026-03-14",
        ],
    }