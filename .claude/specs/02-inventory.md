# Module 2 — Inventory & Stock Ledger
**Status:** Spec Approved | **Build Status:** Not Started

## Problem Statement
Digital stock never matches physical stock in 43% of Indian pharma
depots during inspection. Ghost stock (recorded but missing), phantom
entries, and incorrect batch assignments cause audit failures and
lost sales from products thought to be out of stock but physically present.

## Functional Requirements
FR1: Every stock movement (IN/OUT/RETURN/WRITE_OFF) recorded in real time
FR2: Stock ledger formula: Purchases - Sales - Returns = Current Stock
FR3: Every movement linked to a specific batch (batch-level tracking, not product-level)
FR4: QR scan on carton verifies correct batch before dispatch
FR5: Stock adjustments require photo proof and manager approval
FR6: Ghost stock detection: flag when physical scan differs from ledger
FR7: Multi-location stock view (if depot has multiple godowns)
FR8: Every stock mutation writes to audit_trail automatically via middleware

## API Contracts
GET /inventory/stock/{depot_id}
  Query params: product_id (optional), category (optional)
  Response: [{
    product_id, canonical_name, batches: [{
      batch_id, batch_number, expiry_date, quantity_remaining,
      expiry_risk_score, fefo_rank, status: "active|hold|expired"
    }], total_stock: int
  }]

POST /inventory/stock-in
  Request: { invoice_id, batch_number, product_id, quantity,
             expiry_date, performed_by: user_id }
  Response: { movement_id, batch_id, current_stock_after }
  Side effects: stock_movements (IN), batches record, audit_trail

POST /inventory/stock-out
  Request: { batch_id, quantity, reference_id, performed_by }
  Response: { movement_id, current_stock_after }
  Side effects: stock_movements (OUT), audit_trail

GET /inventory/batch/{batch_id}
  Response: { batch_id, batch_number, product, expiry_date,
              quantity_received, quantity_remaining, quantity_sold,
              movements: [timeline of all IN/OUT/RETURN], status }

POST /inventory/adjustment
  Request: { batch_id, adjusted_quantity, reason, photo_url, performed_by }
  Response: { action_id, approval_status: "PENDING_APPROVAL" }
  Note: requires MANAGER approval before stock_movement created

GET /inventory/stock-ledger/{depot_id}
  Response: matches stock_ledger DB view
  Formula: SUM(IN) - SUM(OUT) - SUM(RETURN) per batch per depot

## Stock Movement Types
IN       → invoice scan confirmed, manual stock-in
OUT      → dispatch confirmed, QR verified
RETURN   → retailer return received
WRITE_OFF → expired, damaged (requires manager approval + photo)
TRANSFER → depot-to-depot (liquidation mode)

## Ghost Stock Detection
Trigger: physical scan quantity ≠ ledger quantity by > 5%
Action: flag batch as ANOMALY_HOLD, raise AnomalyAgent alert
Resolution: manager investigates, approves correction write-off

## Edge Cases
- Partial dispatch: batch has 100 units, dispatch 60 → remaining 40 stays in fefo_rankings
- Split batch: same batch_number, two invoices → merge under one batch_id, log both invoices
- Expired batch not yet written off: flag in expiry radar, exclude from dispatch
- WRITE_OFF of ANOMALY_HOLD batch: requires manager APPROVE not just NOTIFY
- Stock-out below zero: hard block — system rejects movement, returns 409 Conflict

## Acceptance Criteria
- [ ] Stock ledger view = SUM(IN) - SUM(OUT) - SUM(RETURN) verified by test
- [ ] Every movement writes audit_trail entry in same transaction
- [ ] QR scan endpoint returns FEFO rank for scanned batch
- [ ] Stock-out below zero returns 409 Conflict, never creates negative stock
- [ ] Adjustment endpoint creates PENDING_APPROVAL agent action, not immediate movement
- [ ] Ghost stock detection flags batch within one nightly anomaly run
- [ ] Batch timeline endpoint returns full movement history in chronological order