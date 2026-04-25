# Module 3 — Expiry + FEFO Engine
**Status:** Spec Approved | **Build Status:** ML Layer Done

## Problem Statement
Depots lose ₹3-6 lakhs/year to medicines expiring unsold.
Current tools alert at 30 days — too late to act.
FEFO (First Expired First Out) is legally required but warehouse
staff pick whatever is physically accessible, ignoring batch dates.

## Functional Requirements
FR1: System predicts expiry risk (0-1) for every active batch nightly
FR2: System recommends exact liquidation date per at-risk batch
FR3: FEFO ranking updated nightly — earliest expiry dispatched first
FR4: ML override: batch with risk > 0.6 pushed to top regardless of date
FR5: ExpiryPreventionAgent raises SUGGEST_LIQUIDATION when risk > 0.85
FR6: ExpiryPreventionAgent raises FLAG_PRIORITY_DISPATCH when risk > 0.60
FR7: Expiry Radar on dashboard shows all batches color-coded by risk
FR8: Warehouse mobile shows FEFO-ranked pick list with QR scan verification
FR9: Manager can approve/reject liquidation recommendations

## API Contracts
GET /expiry/radar/{depot_id}
  Response: [{ batch_id, product_name, expiry_date, days_left,
               expiry_risk_score, recommended_liquidation_date,
               estimated_loss_inr, risk_band: "critical|warning|ok" }]
  Note: reads from expiry_predictions table (pre-computed at 2 AM)

GET /expiry/fefo-ranking/{depot_id}
  Response: [{ priority_rank, batch_id, product_name, expiry_date,
               remaining_qty, ml_override: bool, days_till_expiry }]

GET /expiry/slow-movers/{depot_id}
  Response: [{ batch_id, product_name, sales_velocity_weekly,
               recommended_liquidation_date, risk_score }]

POST /agents/actions/{id}/approve
  Request: { decided_by: user_id, notes: string }
  Response: { outcome: "APPROVED", decided_at: timestamp }
  Side effects: writes audit_trail, updates retraining_labels view

## Slow Mover Forecaster — Exact Inputs (Problems.pdf spec)
1. sales_velocity_weekly — units sold in last 7 days
2. days_till_expiry — computed from batches.expiry_date
3. seasonality_flag — is product in peak demand season now
4. demand_trend_slope — from DemandForecaster output (negative = falling demand)

## Risk Bands (Expiry Radar Colors)
- Green: risk < 0.40 AND days > 120
- Yellow: risk 0.40-0.60 OR days 60-120
- Orange: risk 0.60-0.85 OR days 30-60
- Red: risk > 0.85 OR days < 30

## FEFO Override Logic
if expiry_risk_score > fefo_override_threshold:  # threshold from compliance_config
    priority_score = expiry_risk_score * -10      # pushes to top
else:
    priority_score = days_till_expiry              # deterministic FEFO

## Edge Cases
- New batch with 0 sales history: velocity = 0, risk computed from time_pressure only
- Cold chain batch (insulin): same risk model, storage temp monitored separately
- Batch flagged ANOMALY_HOLD: excluded from FEFO dispatch list entirely
- ExpiryAgent raises LIQUIDATION, ReorderAgent raises REORDER same product:
  Coordinator suppresses REORDER — never reorder what you're liquidating
- Manager rejects SUGGEST_LIQUIDATION: outcome = REJECTED, feeds retraining labels

## Acceptance Criteria
- [ ] Expiry risk computed for 100% of active batches nightly
- [ ] Liquidation date formula: today + (remaining_stock/daily_velocity) + 14 days
- [ ] FEFO ranking matches legal requirement (expiry_date ASC default)
- [ ] ML override only activates when risk > fefo_override_threshold
- [ ] Agent decision includes estimated_loss_inr in payload
- [ ] ANOMALY_HOLD batches excluded from dispatch plan
- [ ] Manager approval/rejection captured in retraining_labels view
- [ ] Dashboard radar loads in < 200ms (reads pre-computed table)