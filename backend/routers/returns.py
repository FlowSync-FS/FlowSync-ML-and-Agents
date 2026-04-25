"""Return endpoints."""

import logging

from fastapi import APIRouter

logger = logging.getLogger("flowsync.routers.returns")
router = APIRouter()


@router.get("/health")
async def health() -> dict[str, str]:
    """Return router health."""
    logger.info("returns_router: health check")
    return {"router": "returns", "status": "ok"}
