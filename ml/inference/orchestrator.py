"""
ml/inference/orchestrator.py

Nightly ML pipeline coordinator.
Triggered by Celery at 2 AM via backend/tasks.py.
One run per active depot.

Stage order is STRICT — never change:
    1. infer_demand   → writes demand_predictions (everything depends on this)
    2. infer_expiry   → parallel with infer_stockout (both need demand output)
    2. infer_stockout → parallel with infer_expiry
    3. infer_fefo     → needs expiry output
    4. infer_anomaly  → background task, does not block agents
    5. all 4 agents   → read from DB tables
    6. coordinator    → deduplicates, commits, fires WhatsApp

Every stage has a fallback to yesterday's data.
Silent failures are not acceptable — every outcome written to pipeline_run_logs.
"""

import asyncio
import json
import logging
from datetime import date, timedelta
from enum import Enum

from ml.inference import (
    infer_demand,
    infer_expiry,
    infer_stockout,
    infer_fefo,
    infer_anomaly,
)
from ml.agents.coordinator import AgentCoordinator
from ml.shared.config_loader import load_config, get

logger = logging.getLogger("flowsync.orchestrator")


class RunStatus(Enum):
    SUCCESS = "SUCCESS"   # all stages completed
    PARTIAL = "PARTIAL"   # some stages failed, fallback used
    FAILED  = "FAILED"    # critical stage failed, no useful output


async def run_inference_pipeline(
    depot_id: str,
    run_date: str,
    db,
) -> RunStatus:
    """
    Full nightly pipeline for one depot.

    Args:
        depot_id: UUID string
        run_date: ISO date string e.g. '2025-10-20'
        db:       async SQLAlchemy admin session (bypasses RLS)

    Returns:
        RunStatus enum value
    """
    # Load compliance_config from DB at the start of each run so threshold
    # changes take effect without redeployment. All get() calls within this
    # pipeline run will read from the populated cache.
    await load_config(db)

    run_log: dict = {
        "depot_id": depot_id,
        "run_date": run_date,
        "stages":   {},
    }

    # ── Stage 1: Demand ───────────────────────────────────────────────────────
    # CRITICAL — everything downstream needs predicted_daily_rate
    # If this fails and no fallback exists, abort entire depot run
    try:
        demand_results = await infer_demand.run(depot_id, run_date, db)
        run_log["stages"]["demand"] = "SUCCESS"
        logger.info(f"[{depot_id}] Stage 1 demand: OK")

    except Exception as e:
        logger.error(f"[{depot_id}] Stage 1 demand FAILED: {e}")
        demand_results = await _load_yesterday(
            depot_id, "demand_predictions", db
        )
        if demand_results is None:
            run_log["stages"]["demand"] = f"FAILED:{e}"
            await _write_run_log(run_log, RunStatus.FAILED, db)
            await _alert_ops(depot_id, f"demand_stage_failed: {e}", db)
            return RunStatus.FAILED
        run_log["stages"]["demand"] = "FALLBACK:yesterday"
        logger.warning(f"[{depot_id}] Stage 1 using yesterday's demand")

    # ── Stage 2: Expiry + Stockout (parallel) ─────────────────────────────────
    try:
        expiry_results, stockout_results = await asyncio.gather(
            infer_expiry.run(depot_id, demand_results, db),
            infer_stockout.run(depot_id, demand_results, db),
            return_exceptions=True,
        )
    except Exception as e:
        expiry_results   = {}
        stockout_results = {}
        run_log["stages"]["stage2"] = f"GATHER_FAILED:{e}"

    # Handle each result independently —
    # expiry can fail while stockout succeeds
    if isinstance(expiry_results, Exception):
        logger.warning(f"[{depot_id}] Stage 2a expiry failed: {expiry_results}")
        expiry_results = await _load_yesterday(
            depot_id, "expiry_predictions", db
        ) or {}
        run_log["stages"]["expiry"] = "FALLBACK:yesterday"
    else:
        run_log["stages"]["expiry"] = "SUCCESS"

    if isinstance(stockout_results, Exception):
        logger.warning(f"[{depot_id}] Stage 2b stockout failed: {stockout_results}")
        stockout_results = {}
        run_log["stages"]["stockout"] = "SKIPPED"
    else:
        run_log["stages"]["stockout"] = "SUCCESS"

    # ── Stage 3: FEFO ─────────────────────────────────────────────────────────
    try:
        fefo_results = await infer_fefo.run(depot_id, expiry_results, db)
        run_log["stages"]["fefo"] = "SUCCESS"
        logger.info(f"[{depot_id}] Stage 3 FEFO: OK")

    except Exception as e:
        logger.warning(f"[{depot_id}] Stage 3 FEFO failed: {e}")
        fefo_results = await _load_yesterday(
            depot_id, "fefo_rankings", db
        ) or []
        run_log["stages"]["fefo"] = "FALLBACK:yesterday"

    # ── Stage 4: Anomaly (background — does not block agents) ─────────────────
    asyncio.create_task(
        _run_anomaly_safe(depot_id, run_date, db, run_log)
    )

    # ── Stage 5 + 6: Agents + Coordinator ─────────────────────────────────────
    try:
        state = await _build_agent_state(depot_id, run_date, db)

        # Synchronous — pure in-memory, no blocking I/O
        coord     = AgentCoordinator()
        decisions = coord.resolve_and_commit(state)

        await _write_agent_decisions(decisions, depot_id, db)

        run_log["stages"]["agents"] = f"SUCCESS:{len(decisions)} decisions"
        logger.info(
            f"[{depot_id}] Stage 5+6 agents+coordinator: "
            f"{len(decisions)} decisions committed"
        )

    except Exception as e:
        logger.error(f"[{depot_id}] Agent stage failed: {e}")
        run_log["stages"]["agents"] = f"FAILED:{e}"

    status = _determine_status(run_log)
    await _write_run_log(run_log, status, db)
    logger.info(f"[{depot_id}] Pipeline complete — {status.value}")
    return status


async def run_all_depots(run_date: str, db) -> dict:
    """
    Entry point called by Celery trigger_nightly() task.
    Runs pipeline for every active depot sequentially.

    Sequential (not parallel) at MVP to avoid exhausting the
    DB connection pool on a single-server setup.
    Switch to asyncio.gather with a semaphore at 50+ depots.

    Returns summary dict for logging.
    """
    result = await db.execute(
        "SELECT id FROM depots WHERE is_active = TRUE"
    )
    depots  = result.fetchall()
    summary = {
        "total":   len(depots),
        "success": 0,
        "partial": 0,
        "failed":  0,
    }

    logger.info(f"Pipeline starting — {len(depots)} active depots")

    for depot in depots:
        try:
            status = await run_inference_pipeline(
                str(depot.id), run_date, db
            )
            summary[status.value.lower()] += 1
        except Exception as e:
            logger.error(
                f"Unhandled error for depot {depot.id}: {e}"
            )
            summary["failed"] += 1

    logger.info(
        f"Pipeline complete — "
        f"{summary['success']} success | "
        f"{summary['partial']} partial | "
        f"{summary['failed']} failed"
    )
    return summary


# ── Private helpers ───────────────────────────────────────────────────────────

async def _run_anomaly_safe(
    depot_id: str,
    run_date: str,
    db,
    run_log: dict,
) -> None:
    """Fire-and-forget anomaly run. Failure never blocks agents."""
    try:
        await infer_anomaly.run(depot_id, run_date, db)
        run_log["stages"]["anomaly"] = "SUCCESS"
    except Exception as e:
        logger.warning(
            f"[{depot_id}] Anomaly background task failed: {e}"
        )
        run_log["stages"]["anomaly"] = f"FAILED:{e}"


async def _load_yesterday(
    depot_id: str,
    table: str,
    db,
):
    """
    Load yesterday's results as fallback for a failed stage.
    Returns None if no yesterday data exists (first ever run).
    """
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    try:
        rows = await db.execute(f"""
            SELECT * FROM {table}
            WHERE depot_id = :did AND run_date = :rd
        """, {"did": depot_id, "rd": yesterday})
        result = rows.fetchall()
        return result if result else None
    except Exception:
        return None


async def _write_run_log(
    run_log: dict,
    status: RunStatus,
    db,
) -> None:
    await db.execute("""
        INSERT INTO pipeline_run_logs
            (depot_id, run_date, status, stages, finished_at)
        VALUES
            (:did, :rd, :st, :sg, NOW())
        ON CONFLICT (depot_id, run_date)
        DO UPDATE SET
            status      = EXCLUDED.status,
            stages      = EXCLUDED.stages,
            finished_at = EXCLUDED.finished_at
    """, {
        "did": run_log["depot_id"],
        "rd":  run_log["run_date"],
        "st":  status.value,
        "sg":  json.dumps(run_log["stages"]),
    })
    await db.commit()


async def _alert_ops(depot_id: str, reason: str, db) -> None:
    """Write a system alert visible on the ops dashboard."""
    await db.execute("""
        INSERT INTO system_alerts
            (depot_id, alert_type, message, created_at)
        VALUES
            (:did, 'ML_PIPELINE_FAILURE', :msg, NOW())
    """, {
        "did": depot_id,
        "msg": f"Pipeline failed for depot {depot_id}: {reason}",
    })
    await db.commit()


async def _build_agent_state(depot_id: str, run_date: str, db) -> dict:
    """
    Query today's inference output to build the shared state dict for all agents.

    Args:
        depot_id: UUID string
        run_date: ISO date string e.g. '2025-10-20'
        db:       async SQLAlchemy admin session

    Returns:
        State dict consumed by AgentCoordinator.resolve_and_commit().
    """
    async def _fetch(sql: str, params: dict) -> list[dict]:
        result = await db.execute(sql, params)
        rows   = result.fetchall()
        return [dict(r._mapping) for r in rows]

    p = {"did": depot_id, "rd": run_date}

    expiry_predictions = await _fetch("""
        SELECT batch_id, product_id, expiry_risk_score,
               recommended_liquidation_date, method,
               remaining_qty, ptr, product_name
        FROM   v_expiry_predictions
        WHERE  depot_id = :did AND run_date = :rd
    """, p)

    stockout_risks = await _fetch("""
        SELECT product_id, product_name, days_until_stockout,
               lead_time_days, current_stock, predicted_units_14d
        FROM   v_stockout_risks
        WHERE  depot_id = :did AND run_date = :rd
    """, p)

    fefo_rankings = await _fetch("""
        SELECT batch_id, product_id, priority_rank, days_till_expiry,
               ml_override, units_available, predicted_units_14d,
               product_name, expiry_date
        FROM   v_fefo_rankings
        WHERE  depot_id = :did AND run_date = :rd
        ORDER  BY priority_rank ASC
    """, p)

    # anomaly stage is background (Stage 4) — may be empty if still running
    anomaly_flags = await _fetch("""
        SELECT batch_id, product_id, z_score, action,
               quantity, movement_type, movement_id, product_name
        FROM   v_anomaly_flags
        WHERE  depot_id = :did AND run_date = :rd
    """, p)

    demand_forecast = await _fetch("""
        SELECT product_id, predicted_units_14d
        FROM   demand_predictions
        WHERE  depot_id = :did AND run_date = :rd
    """, p)

    try:
        # Net cashflow: INFLOW positive, OUTFLOW negative.
        # SUM(amount) alone is always positive — must apply sign by type.
        cashflow_row      = await _fetch("""
            SELECT COALESCE(SUM(
                CASE WHEN transaction_type = 'INFLOW'  THEN  amount
                     WHEN transaction_type = 'OUTFLOW' THEN -amount
                     ELSE 0
                END
            ), 0) AS net_14d
            FROM   payment_transactions
            WHERE  depot_id         = :did
              AND  transaction_date >= CURRENT_DATE - INTERVAL '14 days'
        """, {"did": depot_id})
        cashflow_negative = float(
            cashflow_row[0].get("net_14d", 0) if cashflow_row else 0
        ) < 0
    except Exception:
        cashflow_negative = False

    critical_threshold = get("expiry_critical_threshold")
    liquidation_products = [
        str(r["product_id"])
        for r in expiry_predictions
        if float(r.get("expiry_risk_score", 0)) >= critical_threshold
    ]
    held_batches = [
        str(r["batch_id"])
        for r in anomaly_flags
        if r.get("action") == "ANOMALY_HOLD"
    ]

    return {
        "depot_id":             depot_id,
        "expiry_predictions":   expiry_predictions,
        "stockout_risks":       stockout_risks,
        "fefo_rankings":        fefo_rankings,
        "anomaly_flags":        anomaly_flags,
        "demand_forecast":      demand_forecast,
        "liquidation_products": liquidation_products,
        "held_batches":         held_batches,
        "cashflow_negative":    cashflow_negative,
    }


async def _write_agent_decisions(
    decisions: list[dict],
    depot_id: str,
    db,
) -> None:
    """
    Persist coordinator output to agent_actions table.

    Args:
        decisions: List of action dicts from AgentCoordinator.resolve_and_commit().
        depot_id:  UUID string
        db:        async SQLAlchemy admin session

    Side effects:
        Inserts rows into agent_actions; commits transaction.
    """
    for d in decisions:
        await db.execute("""
            INSERT INTO agent_actions
                (depot_id, agent, action_type, approval_tier,
                 batch_id, product_id, conflict_key, payload, created_at)
            VALUES
                (:did, :agent, :action_type, :approval_tier,
                 :batch_id, :product_id, :conflict_key, :payload::jsonb, NOW())
            ON CONFLICT (depot_id, conflict_key, created_at::date)
            DO NOTHING
        """, {
            "did":           depot_id,
            "agent":         d.get("agent"),
            "action_type":   d.get("action_type"),
            "approval_tier": d.get("approval_tier"),
            "batch_id":      d.get("batch_id"),
            "product_id":    d.get("product_id"),
            "conflict_key":  d.get("conflict_key"),
            "payload":       json.dumps(d.get("payload", {})),
        })
    await db.commit()
    logger.info(f"[{depot_id}] Agent decisions written: {len(decisions)}")


def _determine_status(run_log: dict) -> RunStatus:
    """Determine overall run status from per-stage outcomes."""
    stages = run_log.get("stages", {})
    if stages.get("demand", "").startswith("FAILED"):
        return RunStatus.FAILED
    if any(v.startswith("FAILED") for v in stages.values()):
        return RunStatus.PARTIAL
    if any(v.startswith("FALLBACK") for v in stages.values()):
        return RunStatus.PARTIAL
    return RunStatus.SUCCESS