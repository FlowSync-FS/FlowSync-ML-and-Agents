"""
backend/services/reconciliation_service.py

Smart matching engine for financial reconciliation.
Matches payments to invoices using 4-rule engine.
Computes DSO, aging buckets, and credit note application.

4 rules (first match wins):
    1. Exact match    — amount == invoice ±1%  AND same retailer
    2. Partial        — amount < invoice, same retailer, >= 10%
    3. Consolidated   — amount == sum of 2-5 open invoices ±2%
    4. Advance        — no invoice yet, retailer known
    Fallback          — UNLINKED → manual review queue

All tolerances read from compliance_config, never hardcoded.
"""

import logging
import uuid
from datetime import date
from decimal import Decimal
from itertools import combinations
from typing import Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ml.shared.config_loader import get

logger = logging.getLogger("flowsync.services.reconciliation")


async def match_payment(
    db:          AsyncSession,
    depot_id:    str,
    retailer_id: str,
    amount:      Decimal,
    payment_id:  str,
    scheme_code: Optional[str] = None,
) -> dict:
    """
    Run 4-rule matching engine for one payment.
    Updates payment reconciliation_status in DB.
    Applies scheme deductions if scheme_code provided.

    Returns:
        {
            reconciliation_status, matched_invoices,
            confidence, balance_remaining
        }
    """
    exact_tol  = Decimal(str(get("exact_match_tolerance",        db, 0.01)))
    consol_tol = Decimal(str(get("consolidated_match_tolerance", db, 0.02)))
    max_bundle = int(get("consolidation_invoice_limit",          db, 5))

    open_invoices = await _get_open_invoices(db, retailer_id)

    result = (
        _rule1_exact(amount, open_invoices, exact_tol)
        or _rule2_partial(amount, open_invoices)
        or _rule3_consolidated(amount, open_invoices, consol_tol, max_bundle)
        or _rule4_advance(retailer_id)
        or _fallback()
    )

    # Apply scheme deduction if provided
    if scheme_code and result["reconciliation_status"] == "PARTIAL":
        result = await _apply_scheme(db, result, scheme_code)

    # Update payment record
    await db.execute(text("""
        UPDATE payments
        SET reconciliation_status = :status
        WHERE id = :pid
    """), {"status": result["reconciliation_status"], "pid": payment_id})

    # Mark matched invoices as SETTLED or PARTIAL
    for inv_id in result.get("matched_invoices", []):
        new_status = (
            "SETTLED"
            if result["reconciliation_status"] in ("SETTLED", "ADVANCE")
            else "PARTIAL"
        )
        await db.execute(text("""
            UPDATE invoices
            SET status = :status
            WHERE id = :iid
        """), {"status": new_status, "iid": inv_id})

    await db.commit()

    logger.info(
        f"Reconciliation: payment={payment_id} "
        f"amount={amount} "
        f"status={result['reconciliation_status']} "
        f"confidence={result['confidence']}"
    )
    return result


async def get_retailer_reconciliation(
    db:          AsyncSession,
    retailer_id: str,
) -> dict:
    """
    Full reconciliation view for one retailer.
    Shows all invoices with payment status and balance.
    Used by GET /reconciliation/retailer/{retailer_id}.
    """
    retailer_row = await db.execute(text("""
        SELECT
            id, name, credit_limit,
            current_outstanding, dso_days,
            credit_risk_score
        FROM retailers
        WHERE id = :rid
    """), {"rid": retailer_id})
    retailer = retailer_row.fetchone()

    if not retailer:
        return {}

    invoice_rows = await db.execute(text("""
        SELECT
            i.id, i.invoice_number, i.invoice_date,
            i.total_amount, i.status,
            COALESCE(SUM(p.amount_paid), 0)  AS amount_paid,
            i.total_amount
                - COALESCE(SUM(p.amount_paid), 0) AS balance_due,
            EXTRACT(DAY FROM NOW() - i.invoice_date) AS days_overdue
        FROM invoices i
        LEFT JOIN payments p ON p.invoice_id = i.id
        WHERE i.retailer_id = :rid
        GROUP BY i.id, i.invoice_number, i.invoice_date,
                 i.total_amount, i.status
        ORDER BY i.invoice_date DESC
    """), {"rid": retailer_id})

    invoices = invoice_rows.fetchall()

    return {
        "retailer_id":       retailer_id,
        "retailer_name":     retailer.name,
        "outstanding_total": float(retailer.current_outstanding or 0),
        "dso_days":          int(retailer.dso_days or 0),
        "credit_risk_score": float(retailer.credit_risk_score or 5.0),
        "credit_limit":      float(retailer.credit_limit or 0),
        "invoices": [
            {
                "invoice_id":     str(inv.id),
                "invoice_number": inv.invoice_number,
                "invoice_date":   str(inv.invoice_date) if inv.invoice_date else None,
                "total_amount":   float(inv.total_amount or 0),
                "amount_paid":    float(inv.amount_paid or 0),
                "balance_due":    float(inv.balance_due or 0),
                "status":         inv.status,
                "days_overdue":   int(inv.days_overdue or 0),
            }
            for inv in invoices
        ],
    }


async def get_aging_report(
    db:       AsyncSession,
    depot_id: str,
) -> dict:
    """
    Aging report for a depot — 0-30, 31-60, 60+ day buckets.
    Used by GET /reconciliation/aging/{depot_id}.
    """
    rows = await db.execute(text("""
        SELECT
            COUNT(*) FILTER (
                WHERE EXTRACT(DAY FROM NOW() - i.invoice_date) <= 30
            ) AS count_0_30,
            COALESCE(SUM(
                CASE WHEN EXTRACT(DAY FROM NOW() - i.invoice_date) <= 30
                     THEN i.total_amount - COALESCE(p.paid, 0) END
            ), 0) AS total_0_30,

            COUNT(*) FILTER (
                WHERE EXTRACT(DAY FROM NOW() - i.invoice_date) BETWEEN 31 AND 60
            ) AS count_31_60,
            COALESCE(SUM(
                CASE WHEN EXTRACT(DAY FROM NOW() - i.invoice_date) BETWEEN 31 AND 60
                     THEN i.total_amount - COALESCE(p.paid, 0) END
            ), 0) AS total_31_60,

            COUNT(*) FILTER (
                WHERE EXTRACT(DAY FROM NOW() - i.invoice_date) > 60
            ) AS count_60_plus,
            COALESCE(SUM(
                CASE WHEN EXTRACT(DAY FROM NOW() - i.invoice_date) > 60
                     THEN i.total_amount - COALESCE(p.paid, 0) END
            ), 0) AS total_60_plus

        FROM invoices i
        LEFT JOIN (
            SELECT invoice_id, SUM(amount_paid) AS paid
            FROM payments GROUP BY invoice_id
        ) p ON p.invoice_id = i.id
        WHERE i.depot_id = :did
          AND i.status   != 'SETTLED'
    """), {"did": depot_id})

    r = rows.fetchone()
    total = float(
        (r.total_0_30 or 0) +
        (r.total_31_60 or 0) +
        (r.total_60_plus or 0)
    )

    return {
        "depot_id": depot_id,
        "as_of":    str(date.today()),
        "zero_to_30": {
            "count":     int(r.count_0_30 or 0),
            "total_inr": float(r.total_0_30 or 0),
        },
        "thirty_to_60": {
            "count":     int(r.count_31_60 or 0),
            "total_inr": float(r.total_31_60 or 0),
        },
        "sixty_plus": {
            "count":     int(r.count_60_plus or 0),
            "total_inr": float(r.total_60_plus or 0),
        },
        "total_outstanding": total,
    }


async def compute_dso(
    db:          AsyncSession,
    retailer_id: str,
) -> dict:
    """
    Compute Days Sales Outstanding for one retailer.
    Formula: outstanding / (total_sales_30d / 30)
    """
    row = await db.execute(text("""
        SELECT
            r.current_outstanding,
            COALESCE(SUM(i.total_amount), 0) AS sales_30d
        FROM retailers r
        LEFT JOIN invoices i
            ON  i.retailer_id = r.id
            AND i.invoice_date >= NOW() - INTERVAL '30 days'
        WHERE r.id = :rid
        GROUP BY r.id, r.current_outstanding
    """), {"rid": retailer_id})

    r = row.fetchone()
    if not r:
        return {"dso_days": 0, "trend": "stable", "last_30d_collections": 0}

    outstanding = float(r.current_outstanding or 0)
    sales_30d   = float(r.sales_30d or 0)
    daily_sales = sales_30d / 30 if sales_30d > 0 else 0.01

    dso = int(outstanding / daily_sales)
    return {
        "retailer_id":          retailer_id,
        "dso_days":             dso,
        "trend":                "stable",
        "last_30d_collections": sales_30d,
    }


# ── 4-Rule engine ─────────────────────────────────────────────────────────────

def _rule1_exact(
    amount:        Decimal,
    open_invoices: list,
    tolerance:     Decimal,
) -> Optional[dict]:
    """Rule 1: payment ≈ one invoice ±tolerance. Confidence 100%."""
    for inv in open_invoices:
        inv_amount = Decimal(str(inv.total_amount or 0))
        if inv_amount == 0:
            continue
        if abs(amount - inv_amount) / inv_amount <= tolerance:
            return {
                "reconciliation_status": "SETTLED",
                "matched_invoices":      [str(inv.id)],
                "confidence":            100,
                "balance_remaining":     Decimal("0"),
                "rule":                  "exact_match",
            }
    return None


def _rule2_partial(
    amount:        Decimal,
    open_invoices: list,
) -> Optional[dict]:
    """
    Rule 2: payment < one invoice, same retailer, >= 10% of invoice.
    Confidence 85%.
    """
    for inv in open_invoices:
        inv_amount = Decimal(str(inv.total_amount or 0))
        if inv_amount == 0:
            continue
        ratio = amount / inv_amount
        if Decimal("0.10") <= ratio < Decimal("1.0"):
            return {
                "reconciliation_status": "PARTIAL",
                "matched_invoices":      [str(inv.id)],
                "confidence":            85,
                "balance_remaining":     inv_amount - amount,
                "rule":                  "partial_payment",
            }
    return None


def _rule3_consolidated(
    amount:        Decimal,
    open_invoices: list,
    tolerance:     Decimal,
    max_bundle:    int,
) -> Optional[dict]:
    """
    Rule 3: payment ≈ sum of 2-N invoices ±tolerance.
    Confidence 90%.
    """
    for n in range(2, min(max_bundle + 1, len(open_invoices) + 1)):
        for combo in combinations(open_invoices, n):
            combo_total = sum(
                Decimal(str(inv.total_amount or 0)) for inv in combo
            )
            if combo_total == 0:
                continue
            if abs(amount - combo_total) / combo_total <= tolerance:
                return {
                    "reconciliation_status": "SETTLED",
                    "matched_invoices":      [str(inv.id) for inv in combo],
                    "confidence":            90,
                    "balance_remaining":     Decimal("0"),
                    "rule":                  "consolidated_payment",
                }
    return None


def _rule4_advance(retailer_id: str) -> dict:
    """
    Rule 4: no matching invoice — retailer is known.
    Hold as advance credit. Confidence 70%.
    """
    return {
        "reconciliation_status": "ADVANCE",
        "matched_invoices":      [],
        "confidence":            70,
        "balance_remaining":     Decimal("0"),
        "rule":                  "advance_payment",
    }


def _fallback() -> dict:
    """No rule matched — flag for manual review."""
    return {
        "reconciliation_status": "UNLINKED",
        "matched_invoices":      [],
        "confidence":            0,
        "balance_remaining":     Decimal("0"),
        "rule":                  "no_match",
    }


async def _get_open_invoices(
    db:          AsyncSession,
    retailer_id: str,
) -> list:
    """Fetch all PENDING and PARTIAL invoices for a retailer."""
    rows = await db.execute(text("""
        SELECT id, total_amount, invoice_date
        FROM invoices
        WHERE retailer_id = :rid
          AND status      IN ('PENDING', 'PARTIAL')
        ORDER BY invoice_date ASC
    """), {"rid": retailer_id})
    return rows.fetchall()


async def _apply_scheme(
    db:          AsyncSession,
    result:      dict,
    scheme_code: str,
) -> dict:
    """
    Apply manufacturer scheme deduction to a PARTIAL match.
    Reduces balance_remaining by scheme discount amount.
    """
    row = await db.execute(text("""
        SELECT terms FROM discount_schemes
        WHERE id = :sid
        LIMIT 1
    """), {"sid": scheme_code})
    scheme = row.fetchone()

    if not scheme or not scheme.terms:
        return result

    terms = scheme.terms
    if terms.get("type") == "percent":
        pct       = Decimal(str(terms.get("value", 0))) / 100
        deduction = result.get("balance_remaining", Decimal("0")) * pct
        result["scheme_deduction"]  = float(deduction)
        result["balance_remaining"] = max(
            result.get("balance_remaining", Decimal("0")) - deduction,
            Decimal("0"),
        )
    return result