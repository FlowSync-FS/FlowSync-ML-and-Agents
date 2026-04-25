"""
backend/database.py

SQLAlchemy async engine + session factory.
RLS (Row-Level Security) context setter.

Two engines:
    engine       — tenant connection, RLS active
                   used by all HTTP routes
    admin_engine — bypasses RLS
                   used ONLY by ML pipeline + Celery tasks + migrations

The RLS pattern:
    FastAPI middleware sets request.state.depot_id from JWT.
    get_db() dependency creates a session and immediately runs:
        SET app.current_depot_id = '{depot_id}'
    PostgreSQL RLS policy on every tenant table uses:
        current_setting('app.current_depot_id', TRUE)::uuid
    Result: a depot can only ever see its own rows, enforced at DB level.
"""

import logging
from contextlib import asynccontextmanager, contextmanager
from typing import AsyncGenerator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy import create_engine

from backend.config import settings

logger = logging.getLogger("flowsync.database")

# ── Tenant engine (HTTP requests) ─────────────────────────────────────────────
engine = create_async_engine(
    settings.database_url,
    pool_size        = 10,
    max_overflow     = 20,
    pool_pre_ping    = True,    # verify connection health before use
    pool_recycle     = 3600,    # recycle connections every hour
    echo             = settings.is_development,  # log SQL in dev only
)

AsyncSessionFactory = async_sessionmaker(
    bind         = engine,
    class_        = AsyncSession,
    expire_on_commit = False,   # avoid lazy-load issues after commit
    autoflush    = False,
)

# ── Admin engine (ML pipeline + Celery + migrations) ─────────────────────────
# Bypasses RLS — NEVER expose through any HTTP route
admin_engine = create_async_engine(
    settings.admin_db_url,
    pool_size     = 5,
    max_overflow  = 10,
    pool_pre_ping = True,
    echo          = False,       # never log admin queries
)

AdminSessionFactory = async_sessionmaker(
    bind             = admin_engine,
    class_           = AsyncSession,
    expire_on_commit = False,
    autoflush        = False,
)

# ── Sync admin engine (Celery tasks — asyncio not available) ──────────────────
sync_admin_engine = create_engine(
    settings.admin_db_url.replace("+asyncpg", "+psycopg2"),
    pool_size     = 3,
    max_overflow  = 5,
    pool_pre_ping = True,
)

SyncAdminSessionFactory = sessionmaker(
    bind             = sync_admin_engine,
    expire_on_commit = False,
    autoflush        = False,
)


# ── SQLAlchemy base ────────────────────────────────────────────────────────────
class Base(DeclarativeBase):
    """
    Base class for all ORM models.
    Import from here: from backend.database import Base
    """
    pass


# ── FastAPI dependency — tenant session ───────────────────────────────────────
async def get_db(request) -> AsyncGenerator[AsyncSession, None]:
    """
    FastAPI dependency injected into every route.
    Sets RLS context variable before yielding session.
    Rolls back on exception, commits on success.

    Usage in router:
        @router.get("/")
        async def handler(db: AsyncSession = Depends(get_db)):
            ...
    """
    depot_id = getattr(request.state, "depot_id", None)

    async with AsyncSessionFactory() as session:
        try:
            if depot_id:
                # Activate RLS for this session
                # All queries in this session will only see
                # rows where depot_id matches
                await session.execute(
                    text(f"SET LOCAL app.current_depot_id = '{depot_id}'")
                )
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


# ── Admin session — async (ML pipeline) ───────────────────────────────────────
@asynccontextmanager
async def get_admin_db() -> AsyncGenerator[AsyncSession, None]:
    """
    Admin session that bypasses RLS.
    Used by ML pipeline orchestrator and Celery async tasks.

    Usage:
        async with get_admin_db() as db:
            await run_inference_pipeline(depot_id, run_date, db)
    """
    async with AdminSessionFactory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


# ── Admin session — sync (Celery tasks) ───────────────────────────────────────
@contextmanager
def get_admin_db_sync():
    """
    Synchronous admin session for Celery tasks.
    Celery workers run in a sync context.

    Usage:
        with get_admin_db_sync() as db:
            depots = db.execute("SELECT id FROM depots").fetchall()
    """
    session = SyncAdminSessionFactory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# ── Health check ───────────────────────────────────────────────────────────────
async def check_db_health() -> bool:
    """
    Used by /health endpoint.
    Returns True if DB is reachable.
    """
    try:
        async with AsyncSessionFactory() as session:
            await session.execute(text("SELECT 1"))
        return True
    except Exception as e:
        logger.error(f"DB health check failed: {e}")
        return False