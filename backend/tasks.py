"""Celery task definitions for FlowSync backend."""

import logging

from celery import Celery

from backend.config import settings

logger = logging.getLogger("flowsync.tasks")

celery_app = Celery("flowsync", broker=settings.redis_url, backend=settings.redis_url)


@celery_app.task(name="flowsync.ping")
def ping() -> str:
    """Return a simple liveness response for workers."""
    return "pong"


@celery_app.task(name="flowsync.pipeline.run")
def run_pipeline_stub(depot_id: str, run_date: str) -> dict[str, str]:
    """Run a safe placeholder pipeline task until full orchestration is wired."""
    logger.info(f"[{depot_id}] pipeline_task: queued for {run_date}")
    return {"status": "queued", "depot_id": depot_id, "run_date": run_date}
