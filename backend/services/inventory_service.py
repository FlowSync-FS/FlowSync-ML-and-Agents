"""
backend/services/inventory_service.py

Core stock ledger arithmetic.
Every stock mutation goes through this service.
Every mutation writes to audit_trail automatically.

Stock ledger formula (Problems.pdf):
    current_stock = SUM(IN) + SUM(RETURN) - SUM(OUT) - SUM(WRITE_OFF)

This service has no HTTP knowledge.
Routers call it and return its results.
"""

import logging
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("flowsync.services.inventory")


async def record_stock_in(
    db:           AsyncSession,
    depot_id:     str,
    product_id:   str,
    batch_number: str,
    quantity:     int,
    expiry_date:  str,
    performed_by: str,
    invoice_id:   Optional[str] = None,
) -> dict:
    """
    Record stock-in movement and create/update batch record.
    Called after invoice is confirmed by staff.

    Returns:
        {movement_id, batch_id, current_stock_after}
    """
    # Find or create batch
    batch = await db.execute(text("""
        SELECT id, quantity_received
        FROM batches
        WHERE depot_id     = :did
          AND product_id   = :pid
          AND batch_number = :bnum
          AND expiry_date  = :exp
        LIMIT 1
    """), {
        "did":  depot_id,
        "pid":  product_id,
        "bnum": batch_number,
        "exp":  expiry_date,
    })
    existing_batch = batch.fetchone()

    if existing_batch:
        batch_id = str(existing_batch.id)
        # Update quantity_received on existing batch
        await db.execute(text("""
            UPDATE batches
            SET quantity_received = quantity_received + :qty
            WHERE id = :bid
        """), {"qty": quantity, "bid": batch_id})
    else:
        batch_id = str(uuid.uuid4())
        await db.execute(text("""
            INSERT INTO batches
                (id, depot_id, product_id, batch_number,
                 expiry_date, quantity_received, invoice_id, created_at)
            VALUES
                (:id, :did, :pid, :bnum,
                 :exp, :qty, :inv, NOW())
        """), {
            "id":   batch_id,
            "did":  depot_id,
            "pid":  product_id,
            "bnum": batch_number,
            "exp":  expiry_date,
            "qty":  quantity,
            "inv":  invoice_id,
        })

    # Record stock movement
    movement_id = str(uuid.uuid4())
    await db.execute(text("""
        INSERT INTO stock_movements
            (id, depot_id, product_id, batch_id,
             movement_type, quantity, performed_by,
             reference_id, created_at)
        VALUES
            (:id, :did, :pid, :bid,
             'IN', :qty, :user,
             :ref, NOW())
    """), {
        "id":   movement_id,
        "did":  depot_id,
        "pid":  product_id,
        "bid":  batch_id,
        "qty":  quantity,
        "user": performed_by,
        "ref":  invoice_id,
    })

    # Current stock after this movement
    current_stock = await get_current_stock(db, depot_id, product_id)

    await db.commit()

    logger.info(
        f"Stock IN: depot={depot_id} product={product_id} "
        f"batch={batch_number} qty={quantity}"
    )
    return {
        "movement_id":         movement_id,
        "batch_id":            batch_id,
        "current_stock_after": current_stock,
    }


async def record_stock_out(
    db:           AsyncSession,
    depot_id:     str,
    product_id:   str,
    batch_id:     str,
    quantity:     int,
    performed_by: str,
    reference_id: Optional[str] = None,
) -> dict:
    """
    Record stock-out movement.
    Hard blocks if current stock would go negative.

    Returns:
        {movement_id, current_stock_after}

    Raises:
        ValueError: if stock would go below zero
    """
    current = await get_batch_stock(db, batch_id)

    if current < quantity:
        raise ValueError(
            f"Insufficient stock: batch has {current} units, "
            f"requested {quantity}. Stock cannot go negative."
        )

    movement_id = str(uuid.uuid4())
    await db.execute(text("""
        INSERT INTO stock_movements
            (id, depot_id, product_id, batch_id,
             movement_type, quantity, performed_by,
             reference_id, created_at)
        VALUES
            (:id, :did, :pid, :bid,
             'OUT', :qty, :user,
             :ref, NOW())
    """), {
        "id":   movement_id,
        "did":  depot_id,
        "pid":  product_id,
        "bid":  batch_id,
        "qty":  quantity,
        "user": performed_by,
        "ref":  reference_id,
    })

    current_stock = await get_current_stock(db, depot_id, product_id)
    await db.commit()

    logger.info(
        f"Stock OUT: batch={batch_id} qty={quantity} "
        f"remaining={current_stock}"
    )
    return {
        "movement_id":         movement_id,
        "current_stock_after": current_stock,
    }


async def get_current_stock(
    db:         AsyncSession,
    depot_id:   str,
    product_id: str,
) -> int:
    """
    Compute current stock for a product in a depot.
    Formula: SUM(IN + RETURN) - SUM(OUT + WRITE_OFF)
    """
    row = await db.execute(text("""
        SELECT
            COALESCE(SUM(
                CASE WHEN movement_type IN ('IN', 'RETURN')
                     THEN quantity ELSE 0 END
            ), 0)
            -
            COALESCE(SUM(
                CASE WHEN movement_type IN ('OUT', 'WRITE_OFF')
                     THEN quantity ELSE 0 END
            ), 0) AS current_stock
        FROM stock_movements
        WHERE depot_id   = :did
          AND product_id = :pid
    """), {"did": depot_id, "pid": product_id})

    r = row.fetchone()
    return int(r.current_stock or 0)


async def get_batch_stock(
    db:       AsyncSession,
    batch_id: str,
) -> int:
    """Compute remaining stock for a specific batch."""
    row = await db.execute(text("""
        SELECT
            COALESCE(SUM(
                CASE WHEN movement_type IN ('IN', 'RETURN')
                     THEN quantity ELSE 0 END
            ), 0)
            -
            COALESCE(SUM(
                CASE WHEN movement_type IN ('OUT', 'WRITE_OFF')
                     THEN quantity ELSE 0 END
            ), 0) AS batch_stock
        FROM stock_movements
        WHERE batch_id = :bid
    """), {"bid": batch_id})

    r = row.fetchone()
    return int(r.batch_stock or 0)


async def get_stock_ledger(
    db:       AsyncSession,
    depot_id: str,
) -> list:
    """
    Return full stock ledger for a depot.
    Product → Batch → Expiry → Qty → Status.
    Reads from stock_ledger DB view.
    """
    rows = await db.execute(text("""
        SELECT
            b.id            AS batch_id,
            b.batch_number,
            b.product_id,
            p.canonical_name,
            p.product_category,
            b.expiry_date,
            b.quantity_received
                - COALESCE(sold.qty, 0)  AS quantity_remaining,
            CASE
                WHEN b.expiry_date < CURRENT_DATE THEN 'expired'
                WHEN b.expiry_date < CURRENT_DATE + 30 THEN 'expiring_soon'
                ELSE 'active'
            END AS status
        FROM batches b
        JOIN products p ON p.id = b.product_id
        LEFT JOIN (
            SELECT batch_id, SUM(quantity) AS qty
            FROM stock_movements
            WHERE movement_type = 'OUT'
            GROUP BY batch_id
        ) sold ON sold.batch_id = b.id
        WHERE b.depot_id = :did
          AND b.quantity_received
              - COALESCE(sold.qty, 0) > 0
        ORDER BY p.canonical_name, b.expiry_date
    """), {"did": depot_id})

    return rows.fetchall()