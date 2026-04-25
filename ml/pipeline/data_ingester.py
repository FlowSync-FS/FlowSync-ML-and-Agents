"""
ml/pipeline/data_ingester.py

Accepts raw client data in any format and returns a
standardised pandas DataFrame.

Handles:
    - CSV (Tally exports, custom formats)
    - Excel (.xlsx, .xls — Marg ERP, custom)
    - JSON

Normalises column names to lowercase snake_case.
Does NOT map to FlowSync schema — that is schema_mapper.py.
Does NOT validate quality — that is data_validator.py.

This file only handles file reading and basic normalisation.
"""

import logging
from pathlib import Path

import pandas as pd

logger = logging.getLogger("flowsync.pipeline.ingester")

# Common date formats seen in Indian pharma Tally / Marg exports
DATE_FORMATS = [
    "%d-%m-%Y",   # 25-10-2025  (most common Tally)
    "%d/%m/%Y",   # 25/10/2025
    "%Y-%m-%d",   # 2025-10-25  (ISO)
    "%d-%b-%Y",   # 25-Oct-2025
    "%d/%m/%y",   # 25/10/25    (2-digit year)
    "%m/%d/%Y",   # 10/25/2025  (US format — some exports)
]


def ingest(file_path: str) -> pd.DataFrame:
    """
    Read a raw data file into a normalised DataFrame.

    Args:
        file_path: absolute or relative path to the data file

    Returns:
        DataFrame with:
            - column names lowercased + spaces replaced with _
            - leading/trailing whitespace stripped from string columns
            - common date columns parsed to datetime where possible

    Raises:
        ValueError: if file format is not supported
        FileNotFoundError: if file does not exist
    """
    path = Path(file_path)

    if not path.exists():
        raise FileNotFoundError(f"Data file not found: {file_path}")

    suffix = path.suffix.lower()

    logger.info(f"Ingesting file: {path.name} ({suffix})")

    if suffix == ".csv":
        df = _read_csv(path)
    elif suffix in [".xlsx", ".xls"]:
        df = _read_excel(path)
    elif suffix == ".json":
        df = _read_json(path)
    else:
        raise ValueError(
            f"Unsupported file format: {suffix}. "
            "Supported: .csv, .xlsx, .xls, .json"
        )

    df = _normalise_columns(df)
    df = _strip_whitespace(df)
    df = _parse_dates(df)

    logger.info(
        f"Ingested {len(df):,} rows, "
        f"{len(df.columns)} columns: {list(df.columns)}"
    )
    return df


def ingest_from_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    """
    Accept an already-loaded DataFrame (e.g. from DB query).
    Still applies normalisation for consistency.
    """
    df = _normalise_columns(df.copy())
    df = _strip_whitespace(df)
    df = _parse_dates(df)
    return df


# ── Private helpers ───────────────────────────────────────────────────────────

def _read_csv(path: Path) -> pd.DataFrame:
    """
    Read CSV. Tries comma delimiter first, then semicolon.
    Pharma Sales (milanzdravkovic) uses comma.
    Some European Tally exports use semicolon.
    """
    try:
        df = pd.read_csv(path, low_memory=False)
        # If only one column, probably semicolon delimited
        if len(df.columns) == 1:
            df = pd.read_csv(path, sep=";", low_memory=False)
        return df
    except Exception as e:
        raise ValueError(f"Failed to read CSV {path.name}: {e}")


def _read_excel(path: Path) -> pd.DataFrame:
    """
    Read Excel. Tries the first sheet by default.
    If first sheet is empty, tries the second sheet.
    Many Tally exports put data on Sheet2.
    """
    try:
        df = pd.read_excel(path, sheet_name=0)
        if df.empty:
            logger.info(f"Sheet 0 empty — trying sheet 1")
            df = pd.read_excel(path, sheet_name=1)
        return df
    except Exception as e:
        raise ValueError(f"Failed to read Excel {path.name}: {e}")


def _read_json(path: Path) -> pd.DataFrame:
    """
    Read JSON. Handles both records format and dict-of-arrays.
    """
    try:
        return pd.read_json(path, orient="records")
    except ValueError:
        try:
            return pd.read_json(path)
        except Exception as e:
            raise ValueError(f"Failed to read JSON {path.name}: {e}")


def _normalise_columns(df: pd.DataFrame) -> pd.DataFrame:
    """
    Lowercase + replace spaces and special chars with underscores.
    'Product Name' → 'product_name'
    'Batch No.'    → 'batch_no_'
    'MRP (₹)'     → 'mrp___'  (cleaned further in schema_mapper)
    """
    df.columns = (
        df.columns
        .str.lower()
        .str.strip()
        .str.replace(r"[^\w]", "_", regex=True)
        .str.replace(r"_+", "_", regex=True)
        .str.strip("_")
    )
    return df


def _strip_whitespace(df: pd.DataFrame) -> pd.DataFrame:
    """Strip leading/trailing whitespace from all string columns."""
    str_cols = df.select_dtypes(include=["object"]).columns
    df[str_cols] = df[str_cols].apply(
        lambda col: col.str.strip() if col.dtype == object else col
    )
    return df


def _parse_dates(df: pd.DataFrame) -> pd.DataFrame:
    """
    Try to parse columns that look like dates.
    Targets columns whose name contains 'date', 'expiry', 'mfg', 'datum'.
    Tries each DATE_FORMAT in order and uses the first that works.
    Non-parseable columns are left as-is.
    """
    date_indicators = ["date", "expiry", "mfg", "datum", "exp", "doa"]
    for col in df.columns:
        if any(ind in col for ind in date_indicators):
            if df[col].dtype == object:
                df[col] = _try_parse_date_column(df[col])
    return df


def _try_parse_date_column(series: pd.Series) -> pd.Series:
    """Try each date format; return parsed series or original on failure."""
    for fmt in DATE_FORMATS:
        try:
            parsed = pd.to_datetime(series, format=fmt, errors="raise")
            logger.debug(f"Parsed date column with format {fmt}")
            return parsed
        except (ValueError, TypeError):
            continue
    # Last resort: let pandas infer
    try:
        return pd.to_datetime(series, errors="coerce")
    except Exception:
        return series   # give up, return as-is


# ── Training dataset loaders ──────────────────────────────────────────────────
# These load Kaggle benchmark datasets used for model training only.
# Production inference uses client data routed through ingest() above.

def load_rossmann(data_dir: str) -> pd.DataFrame:
    """
    Load and merge the Rossmann Store Sales dataset.

    Args:
        data_dir: path to the directory containing train.csv and store.csv
                  (e.g. 'data/rossmann')

    Returns:
        Merged DataFrame with FlowSync column names:
        depot_id, date, units_sold, product_id,
        product_category, depot_region, is_cold_chain
        Filtered to open days with sales > 0, sorted by depot_id + date.

    Raises:
        FileNotFoundError: if train.csv or store.csv not found
    """
    import os

    train_path = os.path.join(data_dir, "train.csv")
    store_path = os.path.join(data_dir, "store.csv")

    for p in [train_path, store_path]:
        if not os.path.exists(p):
            raise FileNotFoundError(f"Rossmann file not found: {p}")

    train  = pd.read_csv(train_path, low_memory=False)
    stores = pd.read_csv(store_path)
    df     = train.merge(stores, on="Store", how="left")

    df = df[(df["Open"] == 1) & (df["Sales"] > 0)].copy()

    df = df.rename(columns={
        "Store":      "depot_id",
        "Date":       "date",
        "Sales":      "units_sold",
        "StoreType":  "product_category",
        "Assortment": "depot_region",
    })

    df["product_id"]    = df["depot_id"].astype(str) + "_generic"
    df["is_cold_chain"] = False
    df["date"]          = pd.to_datetime(df["date"])
    df = df.sort_values(["depot_id", "date"])

    logger.info(
        f"Rossmann loaded: {len(df):,} rows | "
        f"{df['depot_id'].nunique()} stores | "
        f"{df['date'].min().date()} to {df['date'].max().date()}"
    )
    return df


def load_pharma_sales(data_dir: str) -> pd.DataFrame:
    """
    Load the pharma sales dataset (salesdaily.csv).

    Args:
        data_dir: path to directory containing salesdaily.csv
                  (e.g. 'data/pharma_sales')

    Returns:
        DataFrame with columns: datum (datetime), plus drug columns
        (M01AB, M01AE, N02BA, N02BE, N05B, N05C, R03, R06).
        Used by expiry_risk.ipynb for label synthesis.

    Raises:
        FileNotFoundError: if salesdaily.csv not found
    """
    import os

    path = os.path.join(data_dir, "salesdaily.csv")
    if not os.path.exists(path):
        raise FileNotFoundError(f"Pharma sales file not found: {path}")

    df = pd.read_csv(path, sep=",", parse_dates=["datum"])

    logger.info(
        f"Pharma sales loaded: {len(df):,} rows | "
        f"columns: {df.columns.tolist()}"
    )
    return df


def load_otc(data_dir: str) -> pd.DataFrame:
    """
    Load the OTC pharmacy sales dataset.

    Args:
        data_dir: path to directory containing the OTC CSV file
                  (e.g. 'data/pharma_otc_sales')

    Returns:
        DataFrame with normalised columns.
        Exact schema depends on the source CSV.

    Raises:
        FileNotFoundError: if no CSV found in data_dir
    """
    import os
    import glob

    csvs = glob.glob(os.path.join(data_dir, "*.csv"))
    if not csvs:
        raise FileNotFoundError(f"No CSV files found in: {data_dir}")

    path = csvs[0]
    df   = pd.read_csv(path, low_memory=False)
    df   = _normalise_columns(df)

    logger.info(
        f"OTC sales loaded: {len(df):,} rows from {os.path.basename(path)}"
    )
    return df