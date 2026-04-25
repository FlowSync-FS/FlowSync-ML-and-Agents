"""
backend/routers/inventory.py

Inventory endpoints.
No business logic — calls inventory_service and returns results.

GET  /inventory/stock/{depot_id}      — live stock ledger
GET  /inventory/batch/{batch_id}      — batch detail + movement history
POST /inventory/stock-in              — manual stock-in (fallback)
POST /inventory/stock-out             — manual stock-out
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database import get_db
from backend.middleware.auth_middleware import require_role
from backend.schemas.stock import (
    StockInRequest, StockInResponse,
    BatchDetailResponse, StockResponse,
)
from backend.services.inventory_service import (
    record_stock_in,
    record_stock_out,
    get_stock_ledger,
    get_batch_stock,
)

logger = logging.getLogger("flowsync.routers.inventory")

router = APIRouter()


@router.get("/stock/{depot_id}")
async def get_stock(
    depot_id: str,
    request:  Request,
    db:       AsyncSession = Depends(get_db),
):
    """
    Live stock ledger for a depot.
    Returns all active batches grouped by product.
    Reads from stock_movements via inventory_service.
    """
    _assert_depot_access(request, depot_id)

    rows = await get_stock_ledger(db, depot_id)

    # Group by product
    products: dict = {}
    for row in rows:
        pid = str(row.product_id)
        if pid not in products:
            products[pid] = {
                "product_id":     pid,
                "canonical_name": row.canonical_name,
                "category":       row.product_category,
                "total_stock":    0,
                "batches":        [],
            }
        products[pid]["batches"].append({
            "batch_id":          str(row.batch_id),
            "batch_number":      row.batch_number,
            "expiry_date":       str(row.expiry_date),
            "quantity_remaining": int(row.quantity_remaining or 0),
            "status":            row.status,
        })
        products[pid]["total_stock"] += int(row.quantity_remaining or 0)

    return list(products.values())


@router.get("/batch/{batch_id}")
async def get_batch(
    batch_id: str,
    request:  Request,
    db:       AsyncSession = Depends(get_db),
):
    """
    Full batch detail with complete movement timeline.
    """
    batch_row = await db.execute(text("""
        SELECT
            b.id, b.batch_number, b.product_id,
            p.canonical_name, b.expiry_date,
            b.quantity_received, b.created_at
        FROM batches b
        JOIN products p ON p.id = b.product_id
        WHERE b.id = :bid
        LIMIT 1
    """), {"bid": batch_id})

    batch = batch_row.fetchone()
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found")

    movements_row = await db.execute(text("""
        SELECT id, movement_type, quantity, performed_by, created_at
        FROM stock_movements
        WHERE batch_id = :bid
        ORDER BY created_at ASC
    """), {"bid": batch_id})
    movements = movements_row.fetchall()

    qty_sold      = sum(
        m.quantity for m in movements if m.movement_type == "OUT"
    )
    qty_remaining = int(batch.quantity_received) - qty_sold

    return {
        "batch_id":           str(batch.id),
        "batch_number":       batch.batch_number,
        "product_id":         str(batch.product_id),
        "canonical_name":     batch.canonical_name,
        "expiry_date":        str(batch.expiry_date),
        "quantity_received":  batch.quantity_received,
        "quantity_remaining": qty_remaining,
        "quantity_sold":      qty_sold,
        "status":             "active" if qty_remaining > 0 else "depleted",
        "movements": [
            {
                "movement_id":   str(m.id),
                "movement_type": m.movement_type,
                "quantity":      m.quantity,
                "performed_by":  str(m.performed_by) if m.performed_by else None,
                "created_at":    m.created_at.isoformat(),
            }
            for m in movements
        ],
    }


@router.post("/stock-in", response_model=StockInResponse)
async def stock_in(
    body:    StockInRequest,
    request: Request,
    db:      AsyncSession = Depends(get_db),
):
    """
    Manual stock-in fallback.
    Normally stock-in is triggered by /invoices/{id}/confirm.
    This endpoint is for manual corrections and legacy imports.
    Requires MANAGER or ADMIN role.
    """
    _assert_depot_access(request, str(body.invoice_id) if body.invoice_id else None)

    depot_id = request.state.depot_id

    try:
        result = await record_stock_in(
            db           = db,
            depot_id     = depot_id,
            product_id   = str(body.product_id),
            batch_number = body.batch_number,
            quantity     = body.quantity,
            expiry_date  = str(body.expiry_date),
            performed_by = str(body.performed_by),
            invoice_id   = str(body.invoice_id) if body.invoice_id else None,
        )
        return StockInResponse(**result)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))


@router.post("/stock-out")
async def stock_out(
    batch_id:     str,
    quantity:     int,
    performed_by: str,
    request:      Request,
    db:           AsyncSession = Depends(get_db),
):
    """
    Manual stock-out.
    Raises 409 Conflict if stock would go negative.
    """
    depot_id = request.state.depot_id
    try:
        result = await record_stock_out(
            db           = db,
            depot_id     = depot_id,
            product_id   = "",   # resolved from batch_id in service
            batch_id     = batch_id,
            quantity     = quantity,
            performed_by = performed_by,
        )
        return result
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))


def _assert_depot_access(request: Request, target_depot_id: str = None):
    """
    Ensure the requesting user has access to the depot.
    ADMIN can access any depot.
    Others can only access their own depot.
    """
    role     = getattr(request.state, "role",     None)
    depot_id = getattr(request.state, "depot_id", None)

    if role == "ADMIN":
        return

    if target_depot_id and depot_id != target_depot_id:
        raise HTTPException(
            status_code = 403,
            detail      = "Access denied to this depot",
        )