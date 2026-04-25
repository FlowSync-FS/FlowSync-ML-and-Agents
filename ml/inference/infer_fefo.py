"""
ml/inference/infer_fefo.py

FEFORanker — deterministic sort with ML override.
Called by orchestrator.py at Stage 3 (after expiry completes).

Default:  sort by expiry_date ASC (legally required, Schedule M)
Override: if expiry_risk_score > fefo_override_threshold
          → push batch to top of queue regardless of expiry date

The override threshold is read from compliance_config — never hardcoded.
Both conditions must be true for override: risk > threshold.
"""

import logging
from datetime import date

import pandas as pd

from ml.shared.config_loader import get

logger = logging.getLogger("flowsync.infer.fefo")


async def run(
    depot_id: str,
    expiry_results: dict,
    db,
) -> list:
    """
    Rank all active batches by dispatch priority for one depot.

    Args:
        depot_id:       UUID string
        expiry_results: output from infer_expiry.run()
                        {batch_id: {expiry_risk_score, ...}}
        db:             async SQLAlchemy session

    Returns:
        List of batch dicts sorted by priority_score (lowest = dispatch first):
        [{
            batch_id, product_id, expiry_date, remaining_qty,
            expiry_risk_score, ml_override, days_till_expiry,
            priority_score, priority_rank
        }]

    Side effects:
        Deletes old fefo_rankings for this depot+date, inserts fresh ones.
    """
    threshold = get("fefo_override_threshold", db, default=0.6)

    # Fetch all active batches with remaining stock
    batch_rows = await db.execute("""
        SELECT
            b.id::text               AS batch_id,
            b.product_id::text,
            b.expiry_date,
            b.quantity_received
                - COALESCE(
                    SUM(sm.quantity) FILTER (
                        WHERE sm.movement_type = 'OUT'
                    ), 0
                )                    AS remaining_qty
        FROM batches b
        LEFT JOIN stock_movements sm ON sm.batch_id = b.id
        WHERE b.depot_id   = :did
          AND b.expiry_date > NOW()
        GROUP BY b.id, b.product_id, b.expiry_date, b.quantity_received
        HAVING b.quantity_received
            - COALESCE(
                SUM(sm.quantity) FILTER (WHERE sm.movement_type = 'OUT'),
                0
            ) > 0
    """, {"did": depot_id})

    today    = pd.Timestamp.today()
    rankings = []

    for row in batch_rows.fetchall():
        bid          = str(row.batch_id)
        expiry       = pd.Timestamp(row.expiry_date)
        days_to_exp  = int((expiry - today).days)
        remaining    = float(row.remaining_qty or 0)
        risk_score   = expiry_results.get(bid, {}).get(
            "expiry_risk_score", 0.0
        )
        ml_override  = risk_score > threshold

        # Priority score: lower number = dispatched first
        # ML override: negative score pushes batch to top of list
        # Deterministic FEFO: fewer days left = lower score = first out
        if ml_override:
            priority_score = -(risk_score * 1000)   # strongly negative → top
        else:
            priority_score = float(days_to_exp)      # FEFO by expiry date

        rankings.append({
            "batch_id":          bid,
            "product_id":        str(row.product_id),
            "expiry_date":       row.expiry_date.isoformat()
                                 if hasattr(row.expiry_date, "isoformat")
                                 else str(row.expiry_date),
            "remaining_qty":     remaining,
            "expiry_risk_score": round(risk_score, 3),
            "ml_override":       ml_override,
            "days_till_expiry":  days_to_exp,
            "priority_score":    round(priority_score, 4),
        })

    # Sort: lowest priority_score first
    rankings.sort(key=lambda x: x["priority_score"])

    # Assign rank numbers after sort
    for i, r in enumerate(rankings, start=1):
        r["priority_rank"] = i

    override_count = sum(1 for r in rankings if r["ml_override"])
    logger.info(
        f"[{depot_id}] FEFO ranked {len(rankings)} batches | "
        f"{override_count} ML overrides (risk > {threshold})"
    )

    await _write_rankings(depot_id, date.today().isoformat(), rankings, db)
    return rankings


async def _write_rankings(
    depot_id: str,
    run_date: str,
    rankings: list,
    db,
) -> None:
    """
    Replace today's fefo_rankings for this depot.
    Delete old + insert fresh — simpler than upsert for ordered data.
    """
    await db.execute("""
        DELETE FROM fefo_rankings
        WHERE depot_id = :did AND run_date = :rd
    """, {"did": depot_id, "rd": run_date})

    for r in rankings:
        await db.execute("""
            INSERT INTO fefo_rankings
                (depot_id, batch_id, run_date,
                 priority_rank, priority_score, ml_override,
                 expiry_risk_score, days_till_expiry)
            VALUES
                (:did, :bid, :rd,
                 :rank, :score, :ml,
                 :risk, :days)
        """, {
            "did":   depot_id,
            "bid":   r["batch_id"],
            "rd":    run_date,
            "rank":  r["priority_rank"],
            "score": r["priority_score"],
            "ml":    r["ml_override"],
            "risk":  r["expiry_risk_score"],
            "days":  r["days_till_expiry"],
        })

    await db.commit()