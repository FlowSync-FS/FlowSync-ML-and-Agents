# Module 8 — Analytics & Forecasting Dashboard
**Status:** Spec Approved | **Build Status:** ML Layer Done

## Problem Statement
Depot managers make purchasing and stocking decisions based on gut feeling.
They overstock slow-moving products (capital tied up, expiry risk)
and understock fast-moving products (stockouts, lost sales).
They have no visibility into which retailers are payment risks
until money is already overdue.
There is no forward-looking view — only reactive management.

## Functional Requirements
FR1: 30-day demand forecast per product visible on dashboard
FR2: Expiry risk forecast visible as Expiry Radar (color-coded)
FR3: Slow movers report with recommended liquidation dates
FR4: Cash flow forecast: projected collections vs expected payables (30 days)
FR5: Retailer risk scores visible with payment behavior breakdown
FR6: Inventory health score per depot (composite metric)
FR7: Agent decision queue — all pending approvals in one view
FR8: Historical trend charts for key metrics (sales velocity, DSO, expiry rate)
FR9: All charts load from pre-computed tables — zero ML at query time

## API Contracts
GET /analytics/demand-forecast/{depot_id}
  Response: [{
    product_id, canonical_name, category,
    predicted_units_14d, predicted_daily_rate,
    current_stock, days_until_stockout,
    trend: "rising|falling|stable"
  }]
  Source: demand_predictions table (pre-computed 2 AM)

GET /analytics/slow-movers/{depot_id}
  Response: [{
    batch_id, product_name, expiry_date,
    sales_velocity_weekly, risk_score,
    recommended_liquidation_date,
    estimated_loss_if_ignored_inr
  }]
  Source: expiry_predictions table (pre-computed 2 AM)

GET /analytics/inventory-health/{depot_id}
  Response: {
    health_score: int (0-100),
    components: {
      fefo_compliance_rate: float,
      expiry_risk_batches: int,
      anomaly_holds_active: int,
      stockout_risk_products: int,
      unreconciled_payments_inr: decimal
    }
  }

GET /analytics/cashflow-forecast/{depot_id}
  Response: {
    next_30d_projected_collections: decimal,
    next_30d_expected_payables: decimal,
    net_cashflow: decimal,
    overdue_receivables: decimal,
    daily_breakdown: [{ date, projected_in, projected_out }]
  }

GET /analytics/retailer-risk/{depot_id}
  Response: [{
    retailer_id, name, credit_risk_score,
    dso_days, outstanding_inr, credit_limit,
    risk_band: "low|medium|high|critical",
    last_payment_date
  }]

GET /agents/queue/{depot_id}
  Response: [{
    action_id, action_type, approval_tier,
    agent, created_at, payload,
    product_name (if product_id), batch_number (if batch_id),
    estimated_loss_inr (if SUGGEST_LIQUIDATION)
  }]
  Note: only PENDING_APPROVAL records, sorted by COORDINATOR_PRIORITY

## Inventory Health Score Formula
score = 100
- 2 per batch with expiry_risk_score > 0.60
- 5 per batch with expiry_risk_score > 0.85
- 3 per product with days_until_stockout < lead_time
- 10 per active ANOMALY_HOLD
- 1 per ₹10,000 unreconciled receivables > 30 days
- 5 per temperature log gap > 12 hours (cold chain depots only)
Minimum score: 0

## Cashflow Forecast Method (MVP — No ML Model)
Collections: outstanding invoices × historical collection rate per retailer
  collection_rate = payments_last_90d / invoices_last_90d (per retailer)
Payables: pending purchase orders + historical monthly manufacturer payments
No trained model — pure arithmetic on existing data
Add ML cashflow forecaster at Month 6 when payment history is deep enough

## Retailer Risk Bands
Low:      credit_risk_score 1-4, DSO < 30
Medium:   credit_risk_score 4-6, DSO 30-60
High:     credit_risk_score 6-8, DSO 60-90 OR outstanding > 80% credit limit
Critical: credit_risk_score 8-10, DSO > 90 OR outstanding > credit limit

## Dashboard Load Performance
All dashboard endpoints: read from pre-computed tables only
Zero ML inference at query time
Target: < 200ms for any analytics endpoint
Charts: recharts on React frontend, data from API
Refresh: user pulls to refresh (no WebSocket needed at MVP)

## Edge Cases
- New depot with 0 sales history: demand forecast shows "insufficient data" not zeros
- All retailers are new (no payment history): cashflow forecast shows "no data" with disclaimer
- Depot with no cold chain products: cold chain components excluded from health score
- Agent queue empty: show "All clear — no pending decisions" not empty table
- Demand forecast MAPE > 30% for a product: show confidence indicator as "low" on dashboard
- Slow movers report: exclude batches under ANOMALY_HOLD (already being investigated)

## Acceptance Criteria
- [ ] All analytics endpoints respond in < 200ms (pre-computed tables only)
- [ ] Inventory health score formula implemented correctly and tested
- [ ] Agent queue shows only PENDING_APPROVAL, sorted by coordinator priority order
- [ ] Demand forecast shows "insufficient data" for new products (< 30 days history)
- [ ] Slow movers excludes ANOMALY_HOLD batches
- [ ] Cashflow forecast uses per-retailer historical collection rate not global average
- [ ] Retailer risk bands applied correctly in test cases covering all 4 bands
- [ ] Dashboard does not call any ML inference function — reads DB tables only