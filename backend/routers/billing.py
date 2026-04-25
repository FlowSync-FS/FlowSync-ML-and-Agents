"""
backend/routers/billing.py

Billing + OCR endpoints.
POST /invoices/scan           — upload photo, run OCR pipeline
POST /invoices/{id}/confirm   — staff confirms/corrects OCR result
GET  /invoices/{id}/line-items — confirmed line items
GET  /invoices/{id}/duplicate-check — pre-confirm fraud check
"""

import logging

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database import get_db
from backend.schemas.invoices import (
    InvoiceConfirmRequest,
    InvoiceConfirmResponse,
    InvoiceScanResponse,
    DuplicateCheckResponse,
)
from backend.services.ocr_service import (
    process_invoice_scan,
    confirm_invoice,
)

logger = logging.getLogger("flowsync.routers.billing")

router = APIRouter()


@router.post("/scan", response_model=InvoiceScanResponse)
async def scan_invoice(
    request:  Request,
    image:    UploadFile = File(...),
    db:       AsyncSession = Depends(get_db),
):
    """
    Upload invoice photo → run OCR pipeline.
    Returns extracted fields with confidence scores.
    Fields with confidence < 80% are flagged for manual correction.
    Time limit: 5 seconds (enforced in ocr_service).

    Staff workflow:
        1. Tap "Scan Invoice" on mobile
        2. Camera opens
        3. Photo uploaded here
        4. Confirmation screen shows extracted fields
        5. Staff corrects low-confidence fields
        6. Staff taps confirm → POST /invoices/{id}/confirm
    """
    depot_id = request.state.depot_id
    if not depot_id:
        raise HTTPException(status_code=401, detail="Not authenticated")

    # Validate file type
    if image.content_type not in ("image/jpeg", "image/png", "image/webp"):
        raise HTTPException(
            status_code = 422,
            detail      = "Only JPEG, PNG, and WebP images accepted",
        )

    image_bytes = await image.read()

    if len(image_bytes) > 10 * 1024 * 1024:  # 10 MB limit
        raise HTTPException(
            status_code = 413,
            detail      = "Image too large. Maximum 10 MB.",
        )

    try:
        result = await process_invoice_scan(
            db          = db,
            depot_id    = depot_id,
            image_bytes = image_bytes,
            filename    = image.filename or "invoice.jpg",
        )
        return InvoiceScanResponse(**result)
    except Exception as e:
        logger.error(f"Invoice scan failed: {e}")
        raise HTTPException(
            status_code = 500,
            detail      = f"OCR processing failed: {str(e)}",
        )


@router.post("/{invoice_id}/confirm", response_model=InvoiceConfirmResponse)
async def confirm_invoice_endpoint(
    invoice_id: str,
    body:       InvoiceConfirmRequest,
    request:    Request,
    db:         AsyncSession = Depends(get_db),
):
    """
    Confirm OCR result.
    Creates stock_movements (IN), batch_master entries, invoice_line_items.
    Updates invoice status to SETTLED.
    Appends raw OCR text to products.aliases for self-enrichment.

    This is the final step of the stock-in workflow.
    Once confirmed, the invoice cannot be modified.
    """
    depot_id = request.state.depot_id

    # Verify invoice belongs to this depot
    row = await db.execute(text("""
        SELECT id FROM invoices
        WHERE id = :iid AND depot_id = :did
        LIMIT 1
    """), {"iid": invoice_id, "did": depot_id})

    if not row.fetchone():
        raise HTTPException(
            status_code = 404,
            detail      = "Invoice not found",
        )

    try:
        result = await confirm_invoice(
            db           = db,
            depot_id     = depot_id,
            invoice_id   = invoice_id,
            line_items   = [item.model_dump() for item in body.line_items],
            confirmed_by = str(body.confirmed_by),
        )
        return InvoiceConfirmResponse(**result)
    except Exception as e:
        logger.error(f"Invoice confirm failed: {e}")
        raise HTTPException(
            status_code = 500,
            detail      = str(e),
        )


@router.get("/{invoice_id}/line-items")
async def get_line_items(
    invoice_id: str,
    request:    Request,
    db:         AsyncSession = Depends(get_db),
):
    """
    Return all confirmed line items for an invoice.
    Used by dashboard to show what was received.
    """
    depot_id = request.state.depot_id

    rows = await db.execute(text("""
        SELECT
            il.product_id,
            p.canonical_name,
            b.batch_number,
            b.expiry_date,
            il.quantity,
            il.mrp,
            il.ptr,
            il.gst_percent
        FROM invoice_line_items il
        JOIN invoices  i ON i.id  = il.invoice_id
        JOIN products  p ON p.id  = il.product_id
        LEFT JOIN batches b ON b.id = il.batch_id
        WHERE il.invoice_id = :iid
          AND i.depot_id    = :did
        ORDER BY p.canonical_name
    """), {"iid": invoice_id, "did": depot_id})

    items = rows.fetchall()
    if not items:
        raise HTTPException(
            status_code = 404,
            detail      = "Invoice not found or has no confirmed line items",
        )

    return [
        {
            "product_id":     str(item.product_id),
            "canonical_name": item.canonical_name,
            "batch_number":   item.batch_number,
            "expiry_date":    str(item.expiry_date) if item.expiry_date else None,
            "quantity":       item.quantity,
            "mrp":            float(item.mrp or 0),
            "ptr":            float(item.ptr or 0),
            "gst_percent":    float(item.gst_percent or 0),
        }
        for item in items
    ]


@router.get("/{invoice_id}/duplicate-check",
            response_model=DuplicateCheckResponse)
async def duplicate_check(
    invoice_id: str,
    request:    Request,
    db:         AsyncSession = Depends(get_db),
):
    """
    Pre-confirm duplicate check.
    Called before POST /{id}/confirm to warn staff.
    Returns is_duplicate and the conflicting invoice_id if found.
    """
    depot_id = request.state.depot_id

    row = await db.execute(text("""
        SELECT id, invoice_number, is_duplicate_flagged
        FROM invoices
        WHERE id = :iid AND depot_id = :did
        LIMIT 1
    """), {"iid": invoice_id, "did": depot_id})

    invoice = row.fetchone()
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")

    if not invoice.is_duplicate_flagged:
        return DuplicateCheckResponse(is_duplicate=False)

    # Find the conflicting invoice
    conflict = await db.execute(text("""
        SELECT id FROM invoices
        WHERE invoice_number = :num
          AND depot_id       = :did
          AND id            != :iid
          AND created_at    >= NOW() - INTERVAL '7 days'
        LIMIT 1
    """), {
        "num": invoice.invoice_number,
        "did": depot_id,
        "iid": invoice_id,
    })
    conflict_row = conflict.fetchone()

    return DuplicateCheckResponse(
        is_duplicate        = True,
        matching_invoice_id = str(conflict_row.id) if conflict_row else None,
        similarity_score    = 100.0,
        reason              = (
            f"Invoice number {invoice.invoice_number} already "
            "exists within the last 7 days."
        ),
    )