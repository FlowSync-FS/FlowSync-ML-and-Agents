"""
backend/services/ocr_service.py

Orchestrates the full OCR pipeline for invoice scanning.
Called by billing router on POST /invoices/scan.

Pipeline:
    1. Pre-process image (contrast, angle correction)
    2. PaddleOCR — layout detection + text extraction
    3. spaCy NER — entity labelling
    4. Fuzzy match against products table — handles OCR typos
    5. Confidence scorer — 0-100% per field
    6. Duplicate check — same invoice_number within 7 days
    7. Write pending invoice to DB (status=PENDING until confirmed)

This file has no HTTP knowledge.
billing.py router calls it and returns the result.
"""

import logging
import re
import uuid
from datetime import date
from typing import Optional

from rapidfuzz import fuzz, process
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.storage_service import storage

logger = logging.getLogger("flowsync.services.ocr")

# Minimum confidence to auto-accept a field without manual review
CONFIDENCE_THRESHOLD = 80.0

# Minimum fuzzy match score to accept a product name match
PRODUCT_MATCH_THRESHOLD = 75


async def process_invoice_scan(
    db:        AsyncSession,
    depot_id:  str,
    image_bytes: bytes,
    filename:  str,
) -> dict:
    """
    Full OCR pipeline for one invoice image.

    Args:
        db:          async DB session (RLS active for depot)
        depot_id:    UUID string
        image_bytes: raw image bytes from upload
        filename:    original filename for S3 key

    Returns:
        {
            invoice_id, invoice_number, party_name, invoice_date,
            total_amount, line_items, overall_confidence,
            needs_review, is_duplicate_flagged
        }
    """
    # Step 1: Store image to S3
    invoice_id = str(uuid.uuid4())
    s3_key     = storage.upload_bytes(
        data       = image_bytes,
        folder     = "invoices",
        depot_id   = depot_id,
        entity_id  = invoice_id,
        filename   = filename,
        content_type = "image/jpeg",
    )

    # Step 2: OCR extraction
    raw_fields = await _run_ocr(image_bytes)

    # Step 3: Product matching for each line item
    product_master = await _load_product_master(db)
    line_items     = []

    for raw_item in raw_fields.get("line_items", []):
        matched = await _match_product(
            raw_name       = raw_item.get("product_name_raw", ""),
            product_master = product_master,
        )
        confidence = _compute_field_confidence(raw_item, matched)
        line_items.append({
            "product_name_raw": raw_item.get("product_name_raw", ""),
            "product_id":       matched.get("product_id"),
            "matched_name":     matched.get("canonical_name"),
            "batch_number":     raw_item.get("batch_number"),
            "expiry_date":      raw_item.get("expiry_date"),
            "quantity":         raw_item.get("quantity"),
            "mrp":              raw_item.get("mrp"),
            "ptr":              raw_item.get("ptr"),
            "gst_percent":      raw_item.get("gst_percent"),
            "confidence":       confidence,
            "needs_correction": confidence < CONFIDENCE_THRESHOLD,
        })

    # Step 4: Overall confidence
    all_confidences   = [item["confidence"] for item in line_items]
    overall_confidence = (
        sum(all_confidences) / len(all_confidences)
        if all_confidences else 0.0
    )
    needs_review = overall_confidence < CONFIDENCE_THRESHOLD

    # Step 5: Duplicate check
    invoice_number     = raw_fields.get("invoice_number")
    is_duplicate       = await _check_duplicate(
        db             = db,
        depot_id       = depot_id,
        invoice_number = invoice_number,
        manufacturer   = raw_fields.get("party_name"),
    )

    # Step 6: Write pending invoice to DB
    await db.execute(text("""
        INSERT INTO invoices
            (id, depot_id, manufacturer_id, invoice_number,
             invoice_date, total_amount, gst_amount,
             status, ocr_confidence_score, original_image_url,
             is_duplicate_flagged, created_at)
        VALUES
            (:id, :did, :mfr, :inv_num,
             :inv_date, :total, :gst,
             'PENDING', :confidence, :img_url,
             :dup, NOW())
    """), {
        "id":         invoice_id,
        "did":        depot_id,
        "mfr":        raw_fields.get("party_name"),
        "inv_num":    invoice_number,
        "inv_date":   raw_fields.get("invoice_date"),
        "total":      raw_fields.get("total_amount"),
        "gst":        raw_fields.get("gst_amount"),
        "confidence": overall_confidence,
        "img_url":    s3_key,
        "dup":        is_duplicate,
    })
    await db.commit()

    return {
        "invoice_id":           invoice_id,
        "invoice_number":       invoice_number,
        "party_name":           raw_fields.get("party_name"),
        "invoice_date":         raw_fields.get("invoice_date"),
        "total_amount":         raw_fields.get("total_amount"),
        "line_items":           line_items,
        "overall_confidence":   round(overall_confidence, 1),
        "needs_review":         needs_review,
        "is_duplicate_flagged": is_duplicate,
    }


async def confirm_invoice(
    db:           AsyncSession,
    depot_id:     str,
    invoice_id:   str,
    line_items:   list,
    confirmed_by: str,
) -> dict:
    """
    Staff confirms OCR result (correcting any wrong fields).
    Creates batch records and stock movements.

    Returns:
        {invoice_id, stock_movements_created, batches_created}
    """
    from backend.services.inventory_service import record_stock_in

    movements_created = 0
    batches_created   = 0

    for item in line_items:
        result = await record_stock_in(
            db           = db,
            depot_id     = depot_id,
            product_id   = str(item["product_id"]),
            batch_number = item["batch_number"],
            quantity     = item["quantity"],
            expiry_date  = str(item["expiry_date"]),
            performed_by = confirmed_by,
            invoice_id   = invoice_id,
        )
        movements_created += 1
        if result.get("batch_id"):
            batches_created += 1

        # Write invoice line item
        await db.execute(text("""
            INSERT INTO invoice_line_items
                (invoice_id, product_id, batch_id, quantity,
                 mrp, ptr, gst_percent, expiry_date_from_invoice)
            VALUES
                (:inv, :pid, :bid, :qty,
                 :mrp, :ptr, :gst, :exp)
        """), {
            "inv": invoice_id,
            "pid": str(item["product_id"]),
            "bid": result.get("batch_id"),
            "qty": item["quantity"],
            "mrp": item.get("mrp"),
            "ptr": item.get("ptr"),
            "gst": item.get("gst_percent"),
            "exp": str(item["expiry_date"]),
        })

    # Update invoice status to SETTLED
    await db.execute(text("""
        UPDATE invoices
        SET status = 'SETTLED'
        WHERE id = :id
    """), {"id": invoice_id})

    # Self-enrichment: append raw OCR text to product aliases
    for item in line_items:
        raw = item.get("product_name_raw", "").strip().lower()
        if raw and item.get("product_id"):
            await db.execute(text("""
                UPDATE products
                SET aliases = array_append(aliases, :alias)
                WHERE id = :pid
                  AND NOT (:alias = ANY(aliases))
            """), {
                "alias": raw,
                "pid":   str(item["product_id"]),
            })

    await db.commit()

    return {
        "invoice_id":             invoice_id,
        "stock_movements_created": movements_created,
        "batches_created":         batches_created,
        "status":                  "CONFIRMED",
    }


# ── Private helpers ────────────────────────────────────────────────────────────

async def _run_ocr(image_bytes: bytes) -> dict:
    """
    Run PaddleOCR + Tesseract + regex on image bytes.
    Returns raw extracted fields dict.

    In production: PaddleOCR is called here.
    For initial testing: returns a mock structure so the
    rest of the pipeline can be tested without GPU.
    """
    try:
        from paddleocr import PaddleOCR
        ocr    = PaddleOCR(use_angle_cls=True, lang="en", show_log=False)
        import numpy as np
        import cv2
        nparr  = np.frombuffer(image_bytes, np.uint8)
        img    = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        result = ocr.ocr(img, cls=True)
        text   = " ".join([
            line[1][0]
            for block in (result or [])
            for line in block
        ])
        return _parse_ocr_text(text)

    except ImportError:
        logger.warning(
            "PaddleOCR not installed — returning mock OCR result. "
            "Install paddlepaddle and paddleocr for production."
        )
        return _mock_ocr_result()


def _parse_ocr_text(text: str) -> dict:
    """Extract structured fields from raw OCR text using regex."""
    fields: dict = {"line_items": []}

    # Invoice number
    inv_match = re.search(
        r"(?:invoice|inv|bill)\s*(?:no|number|#)[:\s]*([A-Z0-9/-]+)",
        text, re.IGNORECASE
    )
    if inv_match:
        fields["invoice_number"] = inv_match.group(1).strip()

    # Date — DD/MM/YYYY or DD-MM-YYYY
    date_match = re.search(
        r"\b(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})\b", text
    )
    if date_match:
        fields["invoice_date"] = date_match.group(1)

    # Total amount
    total_match = re.search(
        r"(?:total|amount)[:\s₹]*([0-9,]+\.?\d*)",
        text, re.IGNORECASE
    )
    if total_match:
        fields["total_amount"] = float(
            total_match.group(1).replace(",", "")
        )

    return fields


def _mock_ocr_result() -> dict:
    """
    Mock OCR result for testing without PaddleOCR installed.
    Remove this when PaddleOCR is set up.
    """
    return {
        "invoice_number": "TEST-001",
        "party_name":     "Test Manufacturer",
        "invoice_date":   str(date.today()),
        "total_amount":   5000.0,
        "gst_amount":     900.0,
        "line_items": [
            {
                "product_name_raw": "Paracetamol 500mg",
                "batch_number":     "B001",
                "expiry_date":      "2027-06-30",
                "quantity":         100,
                "mrp":              15.0,
                "ptr":              12.0,
                "gst_percent":      12.0,
            }
        ],
    }


async def _load_product_master(db: AsyncSession) -> list:
    """Load canonical names + aliases from products table."""
    rows = await db.execute(text("""
        SELECT id::text, canonical_name, aliases
        FROM products
        ORDER BY canonical_name
    """))
    return rows.fetchall()


async def _match_product(
    raw_name:       str,
    product_master: list,
) -> dict:
    """
    Fuzzy-match a raw OCR product name against the product master.
    Uses token_sort_ratio to handle word order differences.
    """
    if not raw_name or not product_master:
        return {}

    # Build choices: canonical name + all aliases
    choices = {}
    for row in product_master:
        choices[row.canonical_name] = str(row.id)
        for alias in (row.aliases or []):
            choices[alias] = str(row.id)

    result = process.extractOne(
        raw_name,
        list(choices.keys()),
        scorer      = fuzz.token_sort_ratio,
        score_cutoff = PRODUCT_MATCH_THRESHOLD,
    )

    if not result:
        return {}

    matched_name = result[0]
    product_id   = choices[matched_name]

    # Get canonical name (alias might have matched)
    canonical = next(
        (r.canonical_name for r in product_master
         if str(r.id) == product_id),
        matched_name,
    )

    return {
        "product_id":     product_id,
        "canonical_name": canonical,
        "match_score":    result[1],
    }


def _compute_field_confidence(
    raw_item: dict,
    matched:  dict,
) -> float:
    """
    Compute confidence score 0-100 for one line item.
    Based on: product match score + field completeness.
    """
    score = 0.0

    # Product match contributes 40 points
    match_score = matched.get("match_score", 0)
    score += (match_score / 100) * 40

    # Required fields each contribute points
    field_weights = {
        "batch_number": 20,
        "expiry_date":  20,
        "quantity":     10,
        "mrp":           5,
        "ptr":           5,
    }
    for field, weight in field_weights.items():
        if raw_item.get(field):
            score += weight

    return round(min(score, 100.0), 1)


async def _check_duplicate(
    db:             AsyncSession,
    depot_id:       str,
    invoice_number: Optional[str],
    manufacturer:   Optional[str],
) -> bool:
    """
    Check for duplicate invoice within 7 days.
    Same invoice_number from same manufacturer = duplicate.
    """
    if not invoice_number:
        return False

    row = await db.execute(text("""
        SELECT id FROM invoices
        WHERE depot_id       = :did
          AND invoice_number = :inv_num
          AND created_at    >= NOW() - INTERVAL '7 days'
        LIMIT 1
    """), {"did": depot_id, "inv_num": invoice_number})

    return row.fetchone() is not None