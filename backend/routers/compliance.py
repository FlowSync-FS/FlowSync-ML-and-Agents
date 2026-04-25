"""Compliance endpoints."""

import logging

from fastapi import APIRouter

logger = logging.getLogger("flowsync.routers.compliance")
router = APIRouter()


@router.get("/health")
async def health() -> dict[str, str]:
    """Return router health."""
    logger.info("compliance_router: health check")
    return {"router": "compliance", "status": "ok"}
