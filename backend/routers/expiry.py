"""Expiry and FEFO endpoints."""

import logging

from fastapi import APIRouter

logger = logging.getLogger("flowsync.routers.expiry")
router = APIRouter()


@router.get("/health")
async def health() -> dict[str, str]:
    """Return router health."""
    logger.info("expiry_router: health check")
    return {"router": "expiry", "status": "ok"}
