"""Compliance business logic."""

import logging

logger = logging.getLogger("flowsync.services.compliance")


async def build_compliance_snapshot(depot_id: str, open_issues: int) -> dict[str, object]:
    """Build a compact compliance snapshot for dashboards."""
    status = "ok" if open_issues == 0 else "attention"
    logger.info(f"[{depot_id}] compliance_snapshot: issues={open_issues}")
    return {"depot_id": depot_id, "status": status, "open_issues": open_issues}
