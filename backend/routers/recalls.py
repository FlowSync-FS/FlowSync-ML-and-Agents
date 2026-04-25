"""Recall endpoints."""

import logging

from fastapi import APIRouter

logger = logging.getLogger("flowsync.routers.recalls")
router = APIRouter()


@router.get("/health")
async def health() -> dict[str, str]:
    """Return router health."""
    logger.info("recalls_router: health check")
    return {"router": "recalls", "status": "ok"}
