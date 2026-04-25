"""Temperature endpoints."""

import logging

from fastapi import APIRouter

logger = logging.getLogger("flowsync.routers.temperature")
router = APIRouter()


@router.get("/health")
async def health() -> dict[str, str]:
    """Return router health."""
    logger.info("temperature_router: health check")
    return {"router": "temperature", "status": "ok"}
