"""
ml/pipeline/schema_mapper.py

Maps client column names → FlowSync canonical schema.

Handles exports from:
    - Tally ERP
    - Marg ERP
    - Custom Excel sheets
    - FlowSync's own export format (passthrough)

Uses exact matches first, then fuzzy matching for typos.
Unmapped required columns are flagged — not silently dropped.

Output DataFrame has FlowSync canonical column names that
demand_features.py and expiry_features.py expect.
"""

import logging
from typing import Optional

import pandas as pd

try:
    from rapidfuzz import process, fuzz
    _RAPIDFUZZ_AVAILABLE = True
except ImportError:
    _RAPIDFUZZ_AVAILABLE = False

logger = logging.getLogger("flowsync.pipeline.mapper")

# Canonical column → list of known source column names
# Ordered by likelihood (most common source names first)
COLUMN_MAP: dict[str, list[str]] = {
    # Core sales columns
    "date": [
        "date", "voucher_date", "bill_date", "invoice_date",
        "datum", "sale_date", "transaction_date", "vch_date",
    ],
    "product_id": [
        "item_name", "product_name", "item", "medicine_name",
        "drug_name", "product", "item_description", "particulars",
        "stock_item", "material_name",
    ],
    "units_sold": [
        "qty", "quantity", "units", "sale_qty", "quantity_sold",
        "billed_qty", "dispatched_qty", "sold_qty", "nos",
        "quantity_out", "boxes_shipped", "units_sold",
    ],
    "depot_id": [
        "godown", "location", "warehouse", "store",
        "branch", "depot", "site", "stock_location",
    ],
    "batch_number": [
        "batch", "batch_no", "batch_number", "lot_no",
        "batch_no_", "lot", "batch_code",
    ],
    "expiry_date": [
        "expiry", "expiry_date", "exp_date", "expiry_dt",
        "exp", "mfg_exp", "expiration_date",
    ],
    "product_category": [
        "category", "group", "item_group", "drug_type",
        "product_group", "category_name", "therapeutic_category",
        "product_type",
    ],
    "mrp": [
        "mrp", "m_r_p", "max_retail_price", "retail_price",
    ],
    "ptr": [
        "ptr", "p_t_r", "price_to_retailer",
    ],
    "manufacturer": [
        "manufacturer", "company", "mfg", "brand",
        "supplier", "vendor", "mfr_name",
    ],
    "invoice_number": [
        "invoice_no", "invoice_number", "bill_no", "vch_no",
        "voucher_no", "document_no",
    ],
    "quantity_received": [
        "quantity_received", "qty_in", "received_qty",
        "purchase_qty", "quantity_in", "inward_qty",
    ],
}

# Columns required for demand features
REQUIRED_COLUMNS = ["date", "product_id", "units_sold"]

# Fuzzy match threshold — below this score, column is flagged as unmapped
FUZZY_THRESHOLD = 72


def map_schema(
    df: pd.DataFrame,
    client_id: str,
    depot_id_override: Optional[str] = None,
) -> tuple[pd.DataFrame, list[str]]:
    """
    Map source columns to FlowSync canonical schema.

    Args:
        df:                 DataFrame from data_ingester.py
        client_id:          used as default depot_id if none found
        depot_id_override:  explicit depot_id (skips column lookup)

    Returns:
        mapped_df:    DataFrame with FlowSync canonical column names
        warnings:     list of warning messages for unmapped/missing columns
    """
    source_cols = list(df.columns)
    mapped      = {}
    warnings    = []

    for target_col, source_options in COLUMN_MAP.items():
        matched_col = _find_column(source_cols, source_options)

        if matched_col:
            mapped[target_col] = df[matched_col]
            logger.debug(f"Mapped {matched_col!r} → {target_col!r}")
        else:
            # Required column missing — warn but continue
            if target_col in REQUIRED_COLUMNS:
                warnings.append(
                    f"REQUIRED column not found: {target_col!r}. "
                    f"Source columns: {source_cols}"
                )
            else:
                logger.debug(f"Optional column not found: {target_col!r}")

    result = pd.DataFrame(mapped)

    # Apply defaults for missing non-required columns
    if "depot_id" not in result.columns or result["depot_id"].isna().all():
        result["depot_id"] = depot_id_override or client_id

    if "product_category" not in result.columns:
        result["product_category"] = "unknown"

    if "is_cold_chain" not in result.columns:
        result["is_cold_chain"] = False

    if "depot_region" not in result.columns:
        result["depot_region"] = "unknown"

    # Coerce types
    result = _coerce_types(result)

    # Drop rows where required columns are all null
    before = len(result)
    result = result.dropna(subset=[
        
        c for c in REQUIRED_COLUMNS if c in result.columns
    ])
    dropped = before - len(result)
    if dropped:
        warnings.append(
            f"{dropped} rows dropped — missing required column values"
        )

    logger.info(
        f"Schema mapped: {len(result):,} rows | "
        f"{len(warnings)} warnings"
    )
    if warnings:
        for w in warnings:
            logger.warning(f"[{client_id}] {w}")

    return result, warnings


def _find_column(
    source_cols: list[str],
    target_options: list[str],
) -> Optional[str]:
    """
    Find the best matching source column for a target.

    Strategy:
        1. Exact match (case-insensitive)
        2. Fuzzy match with token_sort_ratio >= FUZZY_THRESHOLD
    """
    # Step 1: exact match (case-insensitive)
    source_lower = {c.lower(): c for c in source_cols}
    for option in target_options:
        if option.lower() in source_lower:
            return source_lower[option.lower()]

    # Step 2: fuzzy match (only if rapidfuzz is installed)
    if _RAPIDFUZZ_AVAILABLE:
        best_match = process.extractOne(
            query=target_options[0],
            choices=source_cols,
            scorer=fuzz.token_sort_ratio,
            score_cutoff=FUZZY_THRESHOLD,
        )
        if best_match:
            return best_match[0]

    return None


def _coerce_types(df: pd.DataFrame) -> pd.DataFrame:
    """Coerce columns to their expected types after mapping."""
    numeric_cols = [
        "units_sold", "quantity_received", "mrp", "ptr",
    ]
    for col in numeric_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)

    bool_cols = ["is_cold_chain"]
    for col in bool_cols:
        if col in df.columns:
            df[col] = df[col].astype(bool)

    return df


# ── Training dataset schema mappers ───────────────────────────────────────────
# Each function maps a Kaggle benchmark dataset to FlowSync canonical schema.
# Used by auto_trainer.py and training notebooks — never in production inference.

def map_rossmann(df: pd.DataFrame) -> pd.DataFrame:
    """
    Map the Rossmann Store Sales dataset to FlowSync canonical schema.

    Expects df from data_ingester.load_rossmann() which has already
    renamed Store→depot_id, Date→date, Sales→units_sold, etc.
    This function adds any missing defaults and validates required columns.

    Args:
        df: DataFrame from load_rossmann()

    Returns:
        DataFrame ready for build_demand_features():
        depot_id, date (datetime), units_sold, product_id,
        product_category, depot_region, is_cold_chain
    """
    df = df.copy()

    if "product_id" not in df.columns:
        df["product_id"] = df["depot_id"].astype(str) + "_generic"

    if "product_category" not in df.columns:
        df["product_category"] = "unknown"

    if "depot_region" not in df.columns:
        df["depot_region"] = "unknown"

    if "is_cold_chain" not in df.columns:
        df["is_cold_chain"] = False

    df["date"] = pd.to_datetime(df["date"])

    logger.info(f"map_rossmann: {len(df):,} rows mapped")
    return df


def map_pharma_sales(df: pd.DataFrame) -> pd.DataFrame:
    """
    Map the pharma sales dataset (salesdaily.csv) to a long-format DataFrame.

    Melts the wide drug-column format into rows with product_id and units_sold,
    suitable for demand feature engineering.

    Args:
        df: DataFrame from load_pharma_sales() with datum + drug columns

    Returns:
        Long-format DataFrame with:
        date (datetime), product_id (drug ATC code),
        units_sold (float), depot_id, product_category, is_cold_chain
    """
    df = df.copy()
    df = df.rename(columns={"datum": "date"})
    df["date"] = pd.to_datetime(df["date"])

    drug_cols = [
        c for c in df.columns
        if c not in ["date", "Year", "Month", "Hour", "weekday_name"]
        and df[c].dtype in ["float64", "int64"]
    ]

    melted = df.melt(
        id_vars=["date"],
        value_vars=drug_cols,
        var_name="product_id",
        value_name="units_sold",
    )

    melted["depot_id"]        = "pharma_default"
    melted["product_category"] = melted["product_id"].str[:3]  # ATC level 3
    melted["is_cold_chain"]   = False
    melted["units_sold"]      = pd.to_numeric(melted["units_sold"], errors="coerce").fillna(0)

    logger.info(f"map_pharma_sales: {len(melted):,} rows (melted from {len(drug_cols)} drugs)")
    return melted


def map_otc_sales(df: pd.DataFrame) -> pd.DataFrame:
    """
    Map the OTC pharmacy sales dataset to FlowSync canonical schema.

    Applies generic map_schema() with a default client_id.

    Args:
        df: DataFrame from load_otc()

    Returns:
        Mapped DataFrame with FlowSync canonical columns.
    """
    mapped, warnings = map_schema(df, client_id="otc_default")
    if warnings:
        for w in warnings:
            logger.warning(f"[otc_default] {w}")
    return mapped