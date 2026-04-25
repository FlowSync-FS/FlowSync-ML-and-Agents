"""
train_and_save.py

Trains demand + expiry models and saves them as local pkl files via ModelStore.
Uses the same shared feature files as the notebooks — no code duplication.

Usage:
    python train_and_save.py
    python train_and_save.py --model demand
    python train_and_save.py --model expiry
"""

import argparse
import logging
import os
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

# Ensure repo root is on path
REPO_ROOT = Path(__file__).parent.resolve()
sys.path.insert(0, str(REPO_ROOT))

import numpy as np
import pandas as pd
from sklearn.calibration import CalibratedClassifierCV
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import mean_absolute_percentage_error, roc_auc_score, roc_curve
from sklearn.model_selection import StratifiedKFold
import xgboost as xgb

from ml.features.demand_features import build_demand_features, fill_missing_dates, FEATURE_COLS as DEMAND_FEATURES
from ml.features.expiry_features import build_expiry_features, FEATURE_COLS as EXPIRY_FEATURES
from ml.registry.model_store import ModelStore
from ml.shared.config_loader import _defaults

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s — %(message)s")
logger = logging.getLogger("flowsync.train")


# ── Demand model ─────────────────────────────────────────────────────────────

def train_demand(cfg: dict) -> dict:
    logger.info("=== Training DemandForecaster (XGBoost Booster API) ===")

    data_dir = REPO_ROOT / "data"
    train_path = data_dir / "rossmann" / "train.csv"
    store_path = data_dir / "rossmann" / "store.csv"

    assert train_path.exists(), f"train.csv not found: {train_path}"
    assert store_path.exists(), f"store.csv not found: {store_path}"

    train_raw = pd.read_csv(train_path, low_memory=False)
    store_raw = pd.read_csv(store_path, low_memory=False)
    df = train_raw.merge(store_raw, on="Store", how="left")

    df = df.rename(columns={"Date": "date", "Sales": "units_sold",
                             "StoreType": "product_category", "Assortment": "depot_region"})
    df["date"]          = pd.to_datetime(df["date"], errors="coerce")
    df["product_id"]    = df["Store"].astype(str)
    df["depot_id"]      = df["Store"].astype(str)
    df["is_cold_chain"] = False
    df = df[(df["Open"] == 1) & (df["units_sold"] > 0)].copy()
    df = df.sort_values(["depot_id", "date"]).reset_index(drop=True)
    logger.info(f"Rows after filter: {len(df):,}")

    festival_dates = cfg.get("festival_dates", [])
    mape_target    = cfg.get("mape_target", 0.15)
    xgb_cfg        = cfg.get("xgboost", {})

    logger.info("Filling missing dates…")
    df_filled = fill_missing_dates(df)

    logger.info("Building features…")
    X, y = build_demand_features(df_filled, festival_dates=festival_dates)
    logger.info(f"Feature matrix: {X.shape}  mean y: {y.mean():.2f}")

    cv_params = {
        "objective":        "reg:squarederror",
        "max_depth":        xgb_cfg.get("max_depth",        6),
        "learning_rate":    xgb_cfg.get("learning_rate",    0.05),
        "subsample":        xgb_cfg.get("subsample",        0.8),
        "colsample_bytree": xgb_cfg.get("colsample_bytree", 0.8),
        "min_child_weight": xgb_cfg.get("min_child_weight", 3),
        "tree_method":      "hist",
        "nthread":          4,
        "seed":             42,
        "verbosity":        0,
    }
    num_boost_round = xgb_cfg.get("n_estimators", 500)

    # Chronological train/test split (80/20 by date)
    feature_dates = df_filled.loc[X.index, "date"]
    cutoff        = feature_dates.quantile(0.80)
    train_mask    = feature_dates < cutoff
    X_train, X_test = X.loc[train_mask],  X.loc[~train_mask]
    y_train, y_test = y.loc[train_mask],  y.loc[~train_mask]
    dtrain = xgb.DMatrix(X_train, label=y_train)
    logger.info(f"Train: {len(X_train):,}  Test: {len(X_test):,}  (cutoff {cutoff.date()})")

    logger.info("Running 5-fold CV…")
    cv_result = xgb.cv(
        cv_params, dtrain,
        num_boost_round=num_boost_round,
        nfold=5, metrics="rmse",
        early_stopping_rounds=30,
        verbose_eval=100, seed=42,
    )
    best_n = int(cv_result["test-rmse-mean"].idxmin()) + 1
    logger.info(f"Best num_boost_round: {best_n}")

    # Final model uses XGBRegressor (sklearn API) so inference code can call
    # model.predict(DataFrame) without wrapping in DMatrix.
    logger.info("Training final model (XGBRegressor)…")
    xgb_params = {k: v for k, v in cv_params.items() if k not in ("nthread", "seed", "verbosity")}
    xgb_params.update({
        "n_estimators":          best_n + 50,
        "n_jobs":                4,
        "random_state":          42,
        "verbosity":             0,
        "early_stopping_rounds": 20,
    })
    model = xgb.XGBRegressor(**xgb_params)
    model.fit(X_train, y_train, eval_set=[(X_test, y_test)], verbose=50)

    y_pred = np.clip(model.predict(X_test), 0, None)
    mask   = y_test > 0
    mape   = mean_absolute_percentage_error(y_test[mask], y_pred[mask])
    status = "PASS" if mape <= mape_target else "FAIL"
    logger.info(f"Test MAPE: {mape:.4f} ({mape:.1%}) → {status}  (target ≤{mape_target:.0%})")

    store     = ModelStore()
    saved_key = store.save(model, "demand_global", metadata={
        "mape":           round(mape, 4),
        "best_iteration": model.best_iteration,
        "n_features":     len(DEMAND_FEATURES),
        "feature_cols":   DEMAND_FEATURES,
        "xgb_params":     cv_params,
        "dataset":        "rossmann",
    })
    logger.info(f"Demand model saved → {saved_key}")
    return {"status": status, "mape": round(mape, 4), "path": saved_key}


# ── Expiry model ──────────────────────────────────────────────────────────────

def train_expiry(cfg: dict) -> dict:
    logger.info("=== Training ExpiryRiskModel (RF + CalibratedClassifierCV) ===")

    data_dir   = REPO_ROOT / "data"
    sales_path = data_dir / "pharma_sales" / "salesdaily.csv"
    assert sales_path.exists(), f"salesdaily.csv not found: {sales_path}"

    sales = pd.read_csv(sales_path)
    for dc in ["datum", "date", "Date"]:
        if dc in sales.columns:
            sales[dc] = pd.to_datetime(sales[dc], dayfirst=True, errors="coerce")
            sales.rename(columns={dc: "datum"}, inplace=True)
            break

    drug_cols = [
        c for c in sales.columns
        if sales[c].dtype in ["float64", "int64"]
        and c not in ["Year", "M", "D", "Month", "Hour", "Weekday", "Weekend"]
    ]
    logger.info(f"Drug columns ({len(drug_cols)}): {drug_cols}")

    auc_target  = cfg.get("auc_target",    0.80)
    recall_target = cfg.get("recall_target", 0.85)
    rf_cfg      = cfg.get("random_forest", {})

    SHELF_LIFE   = 365
    CATEGORY_MAP = {
        "M01AB": "pain_relief", "M01AE": "pain_relief",
        "N02BA": "pain_relief", "N02BE": "pain_relief",
        "N05B":  "vitamin",     "N05C":  "vitamin",
        "R03":   "respiratory", "R06":   "respiratory",
    }

    sales["month_period"] = sales["datum"].dt.to_period("M")
    records, movements = [], []

    for drug in drug_cols:
        monthly_totals = sales.groupby("month_period")[drug].sum()
        qty_recv       = max(float(monthly_totals.quantile(0.75)), 1.0)

        for period, grp in sales.groupby("month_period"):
            monthly_sold = float(grp[drug].sum())
            qty_rem      = max(qty_recv - monthly_sold, 0.0)
            created      = pd.Timestamp(str(period.start_time))
            expiry       = created + pd.Timedelta(days=SHELF_LIFE)
            cat          = CATEGORY_MAP.get(drug[:5], "pain_relief")
            bid          = f"{drug}_{period}"

            records.append({
                "batch_id":                bid,
                "product_id":              drug,
                "depot_id":                "pharma_global",
                "expiry_date":             expiry,
                "quantity_received":       round(qty_recv, 2),
                "quantity_remaining":      round(qty_rem, 2),
                "product_category":        cat,
                "is_cold_chain":           cat == "cold_chain",
                "default_shelf_life_days": float(SHELF_LIFE),
                "created_at":              created,
            })
            if monthly_sold > 0:
                movements.append({
                    "batch_id":      bid,
                    "quantity":      monthly_sold,
                    "movement_type": "OUT",
                    "created_at":    created + pd.Timedelta(days=15),
                })

    batches_df   = pd.DataFrame(records)
    movements_df = pd.DataFrame(movements)
    logger.info(f"Batches: {len(batches_df):,}  Movements: {len(movements_df):,}")

    X_all, y_all = build_expiry_features(batches_df, movements_df)
    labelled = y_all.notna()
    X = X_all[labelled].reset_index(drop=True)
    y = y_all[labelled].astype(int).reset_index(drop=True)
    pos_rate = y.mean()
    logger.info(f"Labelled: {len(X):,}  Positive rate: {pos_rate:.1%}")

    rf_params = {
        "n_estimators":     rf_cfg.get("n_estimators",     300),
        "max_depth":        rf_cfg.get("max_depth",        8),
        "class_weight":     "balanced",
        "min_samples_leaf": rf_cfg.get("min_samples_leaf", 5),
        "random_state":     rf_cfg.get("random_state",     42),
        "n_jobs":           -1,
    }

    pos_count    = int(y.sum())
    neg_count    = int((y == 0).sum())
    min_class    = min(pos_count, neg_count)
    n_splits     = max(2, min(5, min_class))
    cal_cv       = max(2, min(3, min_class // 4)) if min_class >= 8 else "prefit"
    cal_method   = "isotonic" if min_class >= 30 else "sigmoid"
    logger.info(f"CV: n_splits={n_splits}  cal={cal_method}  cal_cv={cal_cv}")

    skf, auc_scores = StratifiedKFold(n_splits=n_splits, shuffle=True, random_state=42), []
    for fold, (tr_idx, val_idx) in enumerate(skf.split(X, y), 1):
        X_tr, X_val = X.iloc[tr_idx], X.iloc[val_idx]
        y_tr, y_val = y.iloc[tr_idx], y.iloc[val_idx]
        if y_val.nunique() < 2:
            logger.info(f"  Fold {fold}: only one class in val — skipping")
            continue
        rf = RandomForestClassifier(**rf_params)
        min_tr = int(y_tr.value_counts().min())
        if min_tr < 3:
            rf.fit(X_tr, y_tr)
            cal = rf
        else:
            fold_cal_cv = max(2, min(cal_cv if isinstance(cal_cv, int) else 2, min_tr))
            cal = CalibratedClassifierCV(rf, method=cal_method, cv=fold_cal_cv)
            cal.fit(X_tr, y_tr)
        auc = roc_auc_score(y_val, cal.predict_proba(X_val)[:, 1])
        auc_scores.append(auc)
        logger.info(f"  Fold {fold}/{n_splits}  AUC: {auc:.3f}")

    mean_auc = float(np.mean(auc_scores)) if auc_scores else 0.0
    status   = "PASS" if mean_auc > auc_target else "FAIL"
    logger.info(f"CV AUC: {mean_auc:.3f}  (target > {auc_target})  → {status}")

    # Train final model on full dataset
    min_class_full = int(y.value_counts().min())
    final_cal_cv   = max(2, min(5, pos_count // 4)) if pos_count >= 8 else "prefit"
    rf_final       = RandomForestClassifier(**rf_params)
    if min_class_full < 3:
        model = rf_final
        model.fit(X, y)
    elif final_cal_cv == "prefit":
        rf_final.fit(X, y)
        model = CalibratedClassifierCV(rf_final, method="sigmoid", cv="prefit")
        model.fit(X, y)
    else:
        final_cal_cv = max(2, min(final_cal_cv, min_class_full))
        model = CalibratedClassifierCV(rf_final, method=cal_method, cv=final_cal_cv)
        model.fit(X, y)

    probs = model.predict_proba(X)[:, 1]
    fpr, tpr, thresholds = roc_curve(y, probs)
    recall_mask = tpr >= recall_target
    opt_thresh  = float(thresholds[recall_mask][0]) if recall_mask.any() else 0.5
    logger.info(f"Threshold at recall≥{recall_target:.0%}: {opt_thresh:.4f}")

    store = ModelStore()
    meta  = {
        "auc_cv":             round(mean_auc, 4),
        "optimal_threshold":  round(opt_thresh, 4),
        "recall_target":      recall_target,
        "pos_rate":           round(float(y.mean()), 4),
        "n_training_batches": int(len(y)),
        "feature_cols":       EXPIRY_FEATURES,
        "rf_params":          rf_params,
        "dataset":            "pharma_sales_synthetic_labels",
    }
    model_key  = store.save(model, "expiry_risk", metadata=meta)
    thresh_key = store.save({"threshold": opt_thresh}, "expiry_risk_threshold", metadata=meta)
    logger.info(f"Expiry model saved → {model_key}")
    logger.info(f"Threshold saved    → {thresh_key}")
    return {"status": status, "auc": round(mean_auc, 4),
            "threshold": round(opt_thresh, 4), "path": model_key}


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", choices=["demand", "expiry", "all"], default="all")
    args = parser.parse_args()

    cfg     = _defaults()
    results = {}

    if args.model in ("demand", "all"):
        results["demand"] = train_demand(cfg)

    if args.model in ("expiry", "all"):
        results["expiry"] = train_expiry(cfg)

    print("\n" + "=" * 60)
    print("TRAINING SUMMARY")
    print("=" * 60)
    for name, res in results.items():
        icon = "PASS" if res["status"] == "PASS" else "FAIL"
        print(f"  [{icon}] {name:10s}  {res}")
    print("=" * 60)


if __name__ == "__main__":
    main()
