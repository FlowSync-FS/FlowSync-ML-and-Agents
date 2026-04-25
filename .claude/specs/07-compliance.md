# Module 7 — Compliance & Audit Command Center
**Status:** Spec Approved | **Build Status:** Not Started

## Problem Statement
Drug inspectors can arrive unannounced. Depots must produce specific
registers and reports within hours. Most depots take 2-3 days to
compile these manually from Tally and paper records.
A single violation can result in license suspension (₹50L+ revenue loss)
or criminal prosecution under the Drugs & Cosmetics Act.
GDP non-compliance discovered by manufacturers triggers loss of supply contracts.

## Applicable Regulations
- Drugs & Cosmetics Act 1940, Rule 65 (batch records)
- Schedule M (FEFO, storage conditions, batch traceability)
- WHO Good Distribution Practice Guidelines
- GST Act — invoice records 6 years
- IT Act 2000 — electronic records admissibility (Section 65B)
- CDSCO Drug Recall Guidelines

## Functional Requirements
FR1: Stock Register — real-time, exportable on demand
FR2: Purchase/Sale Register — GST-compliant, batch-wise
FR3: Batch inward/outward register — Schedule M requirement
FR4: Temperature log report — GDP requirement, all photo links embedded
FR5: Expiry report — all batches expiring in next 90 days
FR6: Recall report — one-click trace for any batch, completion certificate
FR7: GST-compliant invoice data export — GSTR format
FR8: Rule 65 compliance report — batch traceability end-to-end
FR9: audit_trail export — immutable log of all system events
FR10: All reports exportable as PDF or CSV

## API Contracts
GET /compliance/stock-register/{depot_id}
  Query params: as_of_date (default today)
  Response: PDF | CSV
  Contents: product-wise, batch-wise stock position with opening/closing balance

GET /compliance/purchase-sale-register/{depot_id}
  Query params: from_date, to_date
  Response: PDF | CSV
  Contents: all purchases (invoices IN) and sales (invoices OUT) with batch details

GET /compliance/batch-register/{depot_id}
  Query params: from_date, to_date
  Response: PDF | CSV
  Contents: every batch received and dispatched, batch number, expiry, quantity

GET /compliance/gdp-report/{depot_id}
  Query params: month, year
  Response: PDF
  Contents: temperature logs, excursion summary, FEFO compliance rate,
            storage condition verification, signature field for manager

GET /compliance/rule65-report/{depot_id}
  Query params: batch_id
  Response: PDF
  Contents: full batch journey from purchase invoice → storage → dispatch → retailer

GET /recalls/initiate
  Request: POST { batch_id, product_id, recall_issued_by,
                  cdsco_reference_number, recall_date, reason }
  Response: { recall_id, affected_depots: int, estimated_qty: int }

GET /recalls/{batch_id}/trace
  Response: {
    batch: { batch_number, product, expiry, quantity_received },
    purchase: { invoice_id, invoice_date, manufacturer },
    dispatches: [{ retailer_name, retailer_phone, retailer_gstin,
                   quantity_dispatched, dispatch_date, invoice_id }],
    current_stock_remaining: int,
    trace_completed_in_seconds: float
  }
  Time limit: must complete in < 10 seconds

POST /recalls/{id}/complete
  Request: { completion_notes, completion_report_url }
  Response: { recall_id, status: "COMPLETED", cdsco_completion_report: PDF }

GET /compliance/audit-trail/{depot_id}
  Query params: from_date, to_date, entity_table (optional), performed_by (optional)
  Response: CSV (audit_trail is too large for PDF)
  Note: read-only, immutable — no filtering that could hide entries

## Recall Trace Algorithm
Given batch_id:
1. Query invoices where batch appears in invoice_line_items
2. Query stock_movements OUT for that batch_id
3. Join with deliveries to get retailer info
4. Return complete retailer list with contact details
Must complete in < 10 seconds for any batch

## Report Generation
Engine: ReportLab (PDF) + CSV writer (CSV)
Storage: S3 at {depot_id}/compliance-reports/{report_type}/{YYYY-MM}/{filename}
Signed URL: expires in 24 hours (inspector downloads within window)
No PDF stored in DB — only S3 key stored

## Legal Admissibility Requirements (IT Act 2000, Section 65B)
Temperature logs: immutable, timestamped, hash-verified
Audit trail: INSERT-only, no modification permitted
All reports: include digital generation timestamp and system version
All photo URLs: S3 Object Lock enabled (30-day protection minimum)

## Edge Cases
- Inspector arrives, internet down: last 7 days of reports cached as PDF in local storage
- Recall for batch split across multiple depots: trace covers only current depot
- GDP report for month with missing temperature logs: report shows gaps clearly (not hidden)
- Audit trail export for date range with 100,000+ entries: stream CSV, never load all in memory
- Rule 65 report for batch with ANOMALY_HOLD: include hold event in batch journey timeline
- CDSCO reference number not yet received: allow recall initiation with placeholder, update later

## Acceptance Criteria
- [ ] Recall trace returns all retailers for a batch in < 10 seconds
- [ ] Stock register matches stock_ledger view exactly
- [ ] Temperature log report embeds photo links (S3 signed URLs)
- [ ] GDP report clearly marks missing log sessions (not silently omitted)
- [ ] audit_trail export is read-only, cannot be filtered to hide entries
- [ ] All PDFs include generation timestamp and depot name in header
- [ ] Recall completion certificate generated on POST /recalls/{id}/complete
- [ ] Rule 65 report shows complete batch journey: purchase → storage → dispatch
- [ ] S3 signed URLs expire in exactly 24 hours
- [ ] CSV streaming used for audit trail (no memory overflow on large date ranges)