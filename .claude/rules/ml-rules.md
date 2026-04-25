# ML Rules — Non-Negotiable

## Model Decisions (Locked)
| Model | Algorithm | Reason |
|---|---|---|
| DemandForecaster | XGBoost Regressor | Beats LSTM on tabular pharma at MVP scale |
| ExpiryRiskModel | RF + CalibratedClassifierCV | Needs calibrated probabilities for threshold logic |
| AnomalyEngine | Z-score → IF at month 6 | Works day one, no training data required |
| ColdChainAnomaly | STL Decomposition | Time-series drift ≠ point anomaly, separate file |

## Pipeline Stage Order (Never Change)
1. infer_demand (XGBoost) → writes demand_predictions
2. infer_expiry + infer_stockout (parallel asyncio.gather)
3. infer_fefo (sort + ML override if risk > 0.6)
4. infer_anomaly (asyncio.create_task — background, non-blocking)
5. All 4 agents sequentially
6. coordinator.resolve_and_commit()

## FEFO Override Condition
Default: sort by expiry_date ASC (legally required, Schedule M)
Override: expiry_risk_score > fefo_override_threshold (read from compliance_config)
BOTH conditions must be true. Never hardcode the 0.6 threshold.

## Agent Priority Order (coordinator.py — Never Reorder)
1. ANOMALY_HOLD
2. SUGGEST_LIQUIDATION
3. FLAG_PRIORITY_DISPATCH
4. SUGGEST_REORDER
5. DISPATCH_PLAN
6. ANOMALY_ALERT

## Approval Tiers
AUTO → no human, no notification
NOTIFY → WhatsApp fires immediately, operation proceeds
APPROVE → operation BLOCKED until manager acts or 24h expires

## Dataset Paths
- Rossmann: data/rossmann/train.csv + store.csv
- Pharma Sales: data/pharma_sales_data/salesdaily.csv
- M5: data/m5_forecasting/sales_train_validation.csv
- OTC: data/pharma_otc_sales/pharmacy_otc_sales_data.csv

## Cold Start Rules
- DemandForecaster: global Kaggle model serves until 90 days client data
- ExpiryRiskModel: formula fallback until 180 days + 50 expired-batch labels
- AnomalyEngine: Z-score always works from day one, no cold start problem