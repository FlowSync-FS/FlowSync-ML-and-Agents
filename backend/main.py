"""
backend/main.py

FastAPI application entry point.
Registers all routers, middleware, startup events.

Run with:
    uvicorn backend.main:app --host 0.0.0.0 --port 8000 --workers 2
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from backend.config   import settings
from backend.database import check_db_health

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level  = settings.log_level,
    format = "%(asctime)s | %(name)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("flowsync.main")


# ── Lifespan (replaces @app.on_event) ─────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown logic."""
    # ── Startup ────────────────────────────────────────────────────────────────
    logger.info(f"FlowSync Health starting — env={settings.env}")

    db_ok = await check_db_health()
    if not db_ok:
        logger.error("Database unreachable at startup — check DATABASE_URL")
    else:
        logger.info("Database connection OK")

    yield

    # ── Shutdown ───────────────────────────────────────────────────────────────
    logger.info("FlowSync Health shutting down")


# ── App ────────────────────────────────────────────────────────────────────────
app = FastAPI(
    title       = "FlowSync Health API",
    description = "AI-powered pharma depot management SaaS",
    version     = "1.0.0",
    docs_url    = "/docs"   if not settings.is_production else None,
    redoc_url   = "/redoc"  if not settings.is_production else None,
    lifespan    = lifespan,
)


# ── CORS ───────────────────────────────────────────────────────────────────────
# In production, restrict origins to your actual domain
app.add_middleware(
    CORSMiddleware,
    allow_origins     = ["*"] if settings.is_development else [
        "https://app.flowsynchealth.com",
        "https://flowsynchealth.com",
    ],
    allow_credentials = True,
    allow_methods     = ["*"],
    allow_headers     = ["*"],
)


# ── Middleware (import after app creation) ─────────────────────────────────────
from backend.middleware.auth_middleware  import AuthMiddleware
from backend.middleware.audit_middleware import AuditMiddleware
from backend.middleware.rate_limit       import RateLimitMiddleware

app.add_middleware(AuthMiddleware)
app.add_middleware(AuditMiddleware)
app.add_middleware(RateLimitMiddleware)


# ── Routers ────────────────────────────────────────────────────────────────────
from backend.routers import (
    auth,
    inventory,
    billing,
    payments,
    returns,
    temperature,
    expiry,
    recalls,
    compliance,
    retailers,
    agents,
    analytics,
)

app.include_router(auth.router,        prefix="/auth",        tags=["Auth"])
app.include_router(inventory.router,   prefix="/inventory",   tags=["Inventory"])
app.include_router(billing.router,     prefix="/invoices",    tags=["Billing + OCR"])
app.include_router(payments.router,    prefix="/payments",    tags=["Payments"])
app.include_router(returns.router,     prefix="/returns",     tags=["Returns"])
app.include_router(temperature.router, prefix="/temperature", tags=["Cold Chain"])
app.include_router(expiry.router,      prefix="/expiry",      tags=["Expiry + FEFO"])
app.include_router(recalls.router,     prefix="/recalls",     tags=["Recalls"])
app.include_router(compliance.router,  prefix="/compliance",  tags=["Compliance"])
app.include_router(retailers.router,   prefix="/retailers",   tags=["Retailers"])
app.include_router(agents.router,      prefix="/agents",      tags=["Agent Queue"])
app.include_router(analytics.router,   prefix="/analytics",   tags=["Analytics"])


# ── Health endpoint ────────────────────────────────────────────────────────────
@app.get("/health", tags=["System"])
async def health():
    """
    Used by Docker health checks and load balancers.
    Returns 200 if API + DB are reachable.
    """
    db_ok = await check_db_health()
    return JSONResponse(
        status_code = 200 if db_ok else 503,
        content     = {
            "status":   "ok"       if db_ok else "degraded",
            "database": "ok"       if db_ok else "unreachable",
            "version":  "1.0.0",
            "env":      settings.env,
        },
    )


# ── Root ───────────────────────────────────────────────────────────────────────
@app.get("/", tags=["System"])
async def root():
    return {
        "product": "FlowSync Health",
        "version": "1.0.0",
        "docs":    "/docs",
    }