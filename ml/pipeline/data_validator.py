"""
ml/pipeline/data_validator.py

Data quality checks before training or inference.
Catches schema issues, excessive nulls, and distribution anomalies early.
"""

import logging
from dataclasses import dataclass, field
from enum import Enum

import pandas as pd

logger = logging.getLogger("flowsync.pipeline.validator")


@dataclass
class ValidationResult:
    passed: bool
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)

    def summary(self) -> str:
        status = "PASSED" if self.passed else "FAILED"
        return (
            f"Validation {status}: "
            f"{len(self.errors)} errors, {len(self.warnings)} warnings"
        )


def validate_sales_df(df: pd.DataFrame) -> ValidationResult:
    """Check a sales DataFrame before demand feature engineering."""
    errors, warnings = [], []

    required = ["product_id", "depot_id", "date", "units_sold"]
    for col in required:
        if col not in df.columns:
            errors.append(f"Missing required column: {col}")

    if errors:
        return ValidationResult(False, errors, warnings)

    # Null checks
    for col in required:
        n = int(df[col].isnull().sum())
        if n > 0:
            pct = n / len(df) * 100
            (errors if pct > 5 else warnings).append(
                f"{col}: {pct:.1f}% nulls"
            )

    # Non-negative sales
    if (df["units_sold"] < 0).any():
        errors.append("units_sold contains negative values")

    # Future dates
    df2 = df.copy()
    df2["date"] = pd.to_datetime(df2["date"])
    if (df2["date"] > pd.Timestamp.today() + pd.Timedelta(days=1)).any():
        warnings.append("Some dates are in the future")

    # Minimum history per product-depot
    min_days = (
        df2.groupby(["product_id", "depot_id"])["date"].count().min()
    )
    if min_days < 30:
        warnings.append(
            f"Some product-depot pairs have < 30 days history "
            f"(min={min_days}). Lag warmup rows will be dropped."
        )

    passed = len(errors) == 0
    result = ValidationResult(passed, errors, warnings)
    logger.info(result.summary())
    return result


def validate_batch_df(df: pd.DataFrame) -> ValidationResult:
    """Check a batch DataFrame before expiry feature engineering."""
    errors, warnings = [], []

    required = ["batch_id", "product_id", "expiry_date"]
    for col in required:
        if col not in df.columns:
            errors.append(f"Missing required column: {col}")

    if not errors:
        expiry = pd.to_datetime(df["expiry_date"])
        n_past = int((expiry < pd.Timestamp.today()).sum())
        if n_past > 0:
            warnings.append(f"{n_past} batches are already expired")

    return ValidationResult(len(errors) == 0, errors, warnings)


class DataSufficiency(Enum):
    """
    Training sufficiency decision from validate().

    Values:
        SUFFICIENT_FOR_BOTH:   ≥ 180 calendar days AND ≥ 50 expired-batch labels
        SUFFICIENT_FOR_DEMAND: ≥ 90 calendar days (demand fine-tune only)
        USE_GLOBAL_MODEL:      < 90 calendar days (deploy global, collect data)
    """
    SUFFICIENT_FOR_BOTH   = "SUFFICIENT_FOR_BOTH"
    SUFFICIENT_FOR_DEMAND = "SUFFICIENT_FOR_DEMAND"
    USE_GLOBAL_MODEL      = "USE_GLOBAL_MODEL"


@dataclass
class ValidationStatus:
    """
    Return type of validate().

    Attributes:
        status:        DataSufficiency enum value
        is_sufficient: True when fine-tuning at least the demand model is viable
        reason:        Human-readable explanation logged and stored in result dict
    """
    status:        DataSufficiency
    is_sufficient: bool
    reason:        str


def validate(df: pd.DataFrame, client_id: str = "") -> ValidationStatus:
    """
    Determine training sufficiency for a client's mapped DataFrame.

    Cold-start rules (ml-rules.md):
        SUFFICIENT_FOR_BOTH:   ≥ 180 calendar days AND ≥ 50 expired-batch labels
        SUFFICIENT_FOR_DEMAND: ≥ 90 calendar days
        USE_GLOBAL_MODEL:      < 90 calendar days

    Args:
        df:        Mapped DataFrame with at minimum product_id, depot_id,
                   date, units_sold columns.
        client_id: UUID string used for logging only.

    Returns:
        ValidationStatus with status, is_sufficient, and reason fields.

    Side effects:
        Logs the sufficiency decision at INFO level.
    """
    if df is None or df.empty:
        return ValidationStatus(
            status=DataSufficiency.USE_GLOBAL_MODEL,
            is_sufficient=False,
            reason="empty_dataframe",
        )

    try:
        dates     = pd.to_datetime(df["date"])
        days_span = int((dates.max() - dates.min()).days)
    except (KeyError, TypeError, ValueError):
        return ValidationStatus(
            status=DataSufficiency.USE_GLOBAL_MODEL,
            is_sufficient=False,
            reason="date_column_missing_or_invalid",
        )

    expired_labels = 0
    if "expiry_date" in df.columns:
        try:
            expired_labels = int(
                (pd.to_datetime(df["expiry_date"]) < pd.Timestamp.today()).sum()
            )
        except (TypeError, ValueError):
            expired_labels = 0

    if days_span >= 180 and expired_labels >= 50:
        status = DataSufficiency.SUFFICIENT_FOR_BOTH
        reason = f"days={days_span} expired_labels={expired_labels}"
    elif days_span >= 90:
        status = DataSufficiency.SUFFICIENT_FOR_DEMAND
        reason = (
            f"days={days_span} expired_labels={expired_labels} "
            "(need 180 days + 50 labels for expiry fine-tune)"
        )
    else:
        status = DataSufficiency.USE_GLOBAL_MODEL
        reason = f"days={days_span} < 90 — insufficient for fine-tuning"

    is_sufficient = status in (
        DataSufficiency.SUFFICIENT_FOR_BOTH,
        DataSufficiency.SUFFICIENT_FOR_DEMAND,
    )

    logger.info(f"[{client_id}] DataSufficiency: {status.value} — {reason}")
    return ValidationStatus(
        status=status,
        is_sufficient=is_sufficient,
        reason=reason,
    )


class DataValidator:
    """
    Facade that runs all validation checks before training or inference.

    Usage:
        result = DataValidator().validate_sales(df)
        result = DataValidator().validate_batches(df)
    """

    def validate_sales(self, df: pd.DataFrame) -> ValidationResult:
        """
        Validate a sales DataFrame for demand feature engineering.

        Args:
            df: DataFrame with at minimum product_id, depot_id, date, units_sold.

        Returns:
            ValidationResult with passed flag, errors list, warnings list.
        """
        return validate_sales_df(df)

    def validate_batches(self, df: pd.DataFrame) -> ValidationResult:
        """
        Validate a batch DataFrame for expiry feature engineering.

        Args:
            df: DataFrame with at minimum batch_id, product_id, expiry_date.

        Returns:
            ValidationResult with passed flag, errors list, warnings list.
        """
        return validate_batch_df(df)
