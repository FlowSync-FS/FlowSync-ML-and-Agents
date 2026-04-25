"""Agent orchestration endpoints."""

import logging

from fastapi import APIRouter

logger = logging.getLogger("flowsync.routers.agents")
router = APIRouter()


@router.get("/health")
async def health() -> dict[str, str]:
    """Return router health."""
    logger.info("agents_router: health check")
    return {"router": "agents", "status": "ok"}
