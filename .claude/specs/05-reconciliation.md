# Module 5 — Financial Reconciliation Engine
**Status:** Spec Approved | **Build Status:** Not Started

## Problem Statement
When a retailer pays ₹45,000 against 3 invoices nobody can match
which payment covers which invoice. Disputes sit unresolved for months.
Depots carry ₹15-20L in unreconciled receivables at any given time.
DSO (Days Sales Outstanding) averages 75-90 days vs target of 30-45 days.

## Functional Requirements
FR1: Field rep records payment with GPS + photo + amount + payment mode
FR2: System auto-matches payment to invoice(s) using 4-rule engine
FR3: Unmatched payments flagged UNLINKED for manual review
FR4: Returns auto-linked to original invoice when created
FR5: Credit notes auto-generated when return is approved (DB trigger)
FR6: DSO computed per retailer updated daily
FR7: Aging report: 0-30, 31-60, 60+ day buckets per retailer
FR8: ReconciliationAgent raises PAYMENT_REMINDER via WhatsApp at 30/60 days
FR9: ReconciliationAgent raises CREDIT_HOLD when DSO > 60 days + outstanding > limit

## API Contracts
POST /payments/record
  Request: { invoice_id: uuid|null, retailer_id, amount, payment_mode,
             gps_lat, gps_lng, photo_url, scheme_code: string|null }
  Response: { payment_id, reconciliation_status, matched_invoices: [uuid],
              confidence: int, balance_remaining: decimal }

GET /reconciliation/retailer/{retailer_id}
  Response: { retailer, outstanding_total, dso_days, credit_risk_score,
              invoices: [{ id, amount, status, days_overdue, payments: [...] }] }

GET /reconciliation/aging/{depot_id}
  Response: { "0_30": { count, total_inr }, "31_60": {...}, "60_plus": {...} }

GET /retailers/{id}/dso
  Response: { dso_days, trend: "improving|worsening|stable", last_30d_collections }

## 4-Rule Matching Engine (First Match Wins)
Rule 1 — Exact: payment ≈ invoice ±1% AND same retailer → SETTLED (100%)
Rule 2 — Partial: payment < invoice, same retailer, ≥10% of invoice → PARTIAL
Rule 3 — Consolidated: payment ≈ sum of 2-5 open invoices ±2% → SETTLED all
Rule 4 — Advance: no invoice, retailer known → ADVANCE (credit balance)
Fallback → UNLINKED → manual review queue

## Scheme Deductions
Applied AFTER matching.
Types: percent (2% post-supply), free_goods (Buy 10 Get 1)
Reduces balance_remaining on PARTIAL match.

## DSO Formula
dso = outstanding_balance / (total_sales_30d / 30)

## Edge Cases
- Same payment amount, two open invoices same value: prompt field rep to specify
- Retailer pays in cash instalments daily for 5 days: each matched as PARTIAL
- Advance payment received before invoice raised: held as credit, auto-applied on invoice
- Disputed invoice: flag as DISPUTED, exclude from aging report, escalate to manager
- GST rounding differences (₹1-3): within ±1% tolerance, auto-settled

## Acceptance Criteria
- [ ] Rule 1 exact match: 100% accuracy on test payment set
- [ ] Rule 3 consolidated: correctly identifies bundle across 2-5 invoices
- [ ] All 4 rules tested in test_reconciliation.py
- [ ] DSO computed correctly for retailer with 6-month payment history
- [ ] Credit note auto-created by DB trigger on return approval
- [ ] WhatsApp reminder queued for invoices > 30 days unpaid
- [ ] CREDIT_HOLD raised by agent when DSO > 60 AND outstanding > credit_limit