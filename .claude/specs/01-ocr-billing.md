# Module 1 — AI Billing + OCR
**Status:** Spec Approved | **Build Status:** Not Started

## Problem Statement
Depot staff manually type invoice data (product name, batch, expiry, qty, MRP)
from paper invoices into Tally. Takes 3-5 minutes per invoice. Error rate ~12%.
Wrong batch numbers cause stock mismatches discovered only during audits.

## Functional Requirements
FR1: Staff can photograph an invoice with mobile camera
FR2: System extracts all line items automatically (product, batch, expiry, qty, MRP, PTR, GST)
FR3: System fuzzy-matches extracted product names against products master
FR4: System scores confidence 0-100% per extracted field
FR5: Fields with confidence < 80% are flagged for manual correction
FR6: On confirm: creates invoice record, batch_master entries, stock_movement (IN)
FR7: System detects duplicate invoices (same invoice_number from same party within 7 days)
FR8: Every confirmed invoice appends raw OCR text to products.aliases (self-enrichment)

## API Contracts
POST /invoices/scan
  Request: multipart/form-data { image: file, depot_id: uuid }
  Response: {
    invoice_id: uuid,
    extracted_fields: { invoice_number, party_name, invoice_date, total_amount },
    line_items: [{ product_name_raw, product_id_matched, batch, expiry, qty, mrp, confidence }],
    overall_confidence: float,
    needs_review: bool,
    is_duplicate_flagged: bool
  }
  Time limit: 5 seconds

POST /invoices/{id}/confirm
  Request: { line_items: [corrected items], confirmed_by: user_id }
  Response: { invoice_id, stock_movements_created: int, batches_created: int }
  Side effects: writes stock_movements (IN), batch_master, invoice_line_items, audit_trail

GET /invoices/{id}/line-items
  Response: [{ product_id, canonical_name, batch_number, expiry_date, qty, mrp, ptr, gst_percent }]

GET /invoices/{id}/duplicate-check
  Response: { is_duplicate: bool, matching_invoice_id: uuid | null, similarity_score: float }

## OCR Pipeline
PaddleOCR (layout) → Tesseract + spaCy NER (text + entities)
→ rapidfuzz token_sort_ratio vs products.aliases (threshold 75)
→ confidence scorer (per field 0-100)
→ dosage mismatch check (if name matches but strength differs → force manual)

## Edge Cases
- Photo taken at angle > 30°: pre-process with contour detection + warp
- Mixed Hindi/English invoice: PaddleOCR handles both natively
- Handwritten batch number: flag entire line for manual entry
- OCR reads "Metforrmin" (double r): fuzzy match catches at score 91
- OCR reads dosage as "650" but matched product is "500mg": FORCE manual regardless of name score
- Duplicate invoice same day: allow with explicit manager override flag

## Acceptance Criteria
- [ ] OCR extracts correct product name for 85%+ of test invoices
- [ ] Duplicate detection catches 100% of exact duplicates within 7 days
- [ ] Confirm endpoint creates stock_movement and batch_master in single transaction
- [ ] Audit trail entry created on every confirm
- [ ] Pipeline completes in < 5 seconds on 95th percentile invoice photo
- [ ] Self-enrichment: alias appended to products table on every confirmed scan