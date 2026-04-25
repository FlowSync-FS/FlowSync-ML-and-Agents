"""Analytics endpoints."""

import logging

from fastapi import APIRouter

logger = logging.getLogger("flowsync.routers.analytics")
router = APIRouter()


@router.get("/health")
async def health() -> dict[str, str]:
    """Return router health."""
    logger.info("analytics_router: health check")
    return {"router": "analytics", "status": "ok"}
