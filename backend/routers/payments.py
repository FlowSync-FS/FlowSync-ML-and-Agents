"""Payment endpoints."""

import logging

from fastapi import APIRouter

logger = logging.getLogger("flowsync.routers.payments")
router = APIRouter()


@router.get("/health")
async def health() -> dict[str, str]:
    """Return router health."""
    logger.info("payments_router: health check")
    return {"router": "payments", "status": "ok"}
