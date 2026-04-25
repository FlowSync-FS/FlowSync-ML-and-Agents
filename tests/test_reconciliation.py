"""
tests/test_reconciliation.py

Unit tests for backend/services/reconciliation_service.py

Tests all 4 reconciliation rules exhaustively.
Covers edge cases: GST rounding, consolidated bundles,
scheme deductions, advance payments.
"""

from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from backend.services.reconciliation_service import (
    _rule1_exact,
    _rule2_partial,
    _rule3_consolidated,
    _rule4_advance,
    _fallback,
)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_invoice(amount: float, inv_id: str = "inv_001") -> MagicMock:
    inv              = MagicMock()
    inv.id           = inv_id
    inv.total_amount = amount
    return inv


# ── Rule 1: Exact match ────────────────────────────────────────────────────────

class TestRule1ExactMatch:

    def test_exact_amount_matches(self):
        """Exact amount → SETTLED, confidence 100."""
        invoices = [_make_invoice(1000.0, "inv_1")]
        result   = _rule1_exact(
            Decimal("1000.00"), invoices, Decimal("0.01")
        )
        assert result is not None
        assert result["reconciliation_status"] == "SETTLED"
        assert result["confidence"] == 100
        assert result["matched_invoices"] == ["inv_1"]

    def test_within_1_percent_tolerance(self):
        """Payment within ±1% of invoice → SETTLED."""
        invoices = [_make_invoice(1000.0, "inv_2")]
        # 1005 is 0.5% above 1000 — within 1% tolerance
        result   = _rule1_exact(
            Decimal("1005.00"), invoices, Decimal("0.01")
        )
        assert result is not None
        assert result["reconciliation_status"] == "SETTLED"

    def test_gst_rounding_within_tolerance(self):
        """
        Indian GST rounding often causes ±₹1-3 differences.
        These must be accepted as exact matches.
        """
        invoices = [_make_invoice(5000.0, "inv_3")]
        # ₹4997 is 0.06% below ₹5000 — well within 1%
        result   = _rule1_exact(
            Decimal("4997.00"), invoices, Decimal("0.01")
        )
        assert result is not None
        assert result["reconciliation_status"] == "SETTLED"

    def test_above_tolerance_no_match(self):
        """Payment 5% above invoice → no exact match."""
        invoices = [_make_invoice(1000.0, "inv_4")]
        result   = _rule1_exact(
            Decimal("1050.00"), invoices, Decimal("0.01")
        )
        assert result is None

    def test_wrong_amount_no_match(self):
        """Completely different amount → no match."""
        invoices = [_make_invoice(1000.0, "inv_5")]
        result   = _rule1_exact(
            Decimal("500.00"), invoices, Decimal("0.01")
        )
        assert result is None

    def test_multiple_invoices_matches_correct_one(self):
        """With multiple open invoices, matches the correct one."""
        invoices = [
            _make_invoice(500.0,  "inv_wrong"),
            _make_invoice(1000.0, "inv_correct"),
            _make_invoice(2000.0, "inv_wrong_2"),
        ]
        result = _rule1_exact(
            Decimal("1000.00"), invoices, Decimal("0.01")
        )
        assert result is not None
        assert result["matched_invoices"] == ["inv_correct"]


# ── Rule 2: Partial payment ────────────────────────────────────────────────────

class TestRule2PartialPayment:

    def test_partial_payment_above_10_percent(self):
        """Payment that is 50% of invoice → PARTIAL."""
        invoices = [_make_invoice(1000.0, "inv_1")]
        result   = _rule2_partial(Decimal("500.00"), invoices)

        assert result is not None
        assert result["reconciliation_status"] == "PARTIAL"
        assert result["balance_remaining"] == Decimal("500.00")
        assert result["confidence"] == 85

    def test_minimum_10_percent_accepted(self):
        """Exactly 10% of invoice → PARTIAL."""
        invoices = [_make_invoice(1000.0, "inv_2")]
        result   = _rule2_partial(Decimal("100.00"), invoices)

        assert result is not None
        assert result["reconciliation_status"] == "PARTIAL"

    def test_below_10_percent_no_match(self):
        """Less than 10% of invoice → no partial match."""
        invoices = [_make_invoice(1000.0, "inv_3")]
        result   = _rule2_partial(Decimal("50.00"), invoices)

        assert result is None

    def test_balance_remaining_correct(self):
        """balance_remaining = invoice_amount - payment_amount."""
        invoices = [_make_invoice(2000.0, "inv_4")]
        result   = _rule2_partial(Decimal("800.00"), invoices)

        assert result is not None
        assert result["balance_remaining"] == Decimal("1200.00")

    def test_payment_equal_to_invoice_no_partial(self):
        """
        Payment exactly equal to invoice should not match Rule 2.
        Rule 2 is for amount < invoice only.
        """
        invoices = [_make_invoice(1000.0, "inv_5")]
        result   = _rule2_partial(Decimal("1000.00"), invoices)

        assert result is None


# ── Rule 3: Consolidated payment ──────────────────────────────────────────────

class TestRule3ConsolidatedPayment:

    def test_two_invoice_bundle(self):
        """Payment matching sum of 2 invoices → SETTLED both."""
        invoices = [
            _make_invoice(1000.0, "inv_1"),
            _make_invoice(2000.0, "inv_2"),
        ]
        result = _rule3_consolidated(
            Decimal("3000.00"), invoices,
            Decimal("0.02"), 5
        )

        assert result is not None
        assert result["reconciliation_status"] == "SETTLED"
        assert result["confidence"] == 90
        assert set(result["matched_invoices"]) == {"inv_1", "inv_2"}

    def test_three_invoice_bundle(self):
        """Payment matching sum of 3 invoices → SETTLED all three."""
        invoices = [
            _make_invoice(500.0,  "inv_1"),
            _make_invoice(750.0,  "inv_2"),
            _make_invoice(1250.0, "inv_3"),
        ]
        result = _rule3_consolidated(
            Decimal("2500.00"), invoices,
            Decimal("0.02"), 5
        )

        assert result is not None
        assert len(result["matched_invoices"]) == 3

    def test_within_2_percent_tolerance(self):
        """
        Consolidated payment within ±2% of invoice bundle total → SETTLED.
        Covers scheme deductions on bulk payments.
        """
        invoices = [
            _make_invoice(1000.0, "inv_1"),
            _make_invoice(1000.0, "inv_2"),
        ]
        # 1980 is 1% below 2000 — within 2% consolidated tolerance
        result = _rule3_consolidated(
            Decimal("1980.00"), invoices,
            Decimal("0.02"), 5
        )

        assert result is not None
        assert result["reconciliation_status"] == "SETTLED"

    def test_exceeds_max_bundle_size_not_matched(self):
        """
        If max_bundle=2, should not try 3-invoice combinations.
        """
        invoices = [
            _make_invoice(100.0, f"inv_{i}")
            for i in range(5)
        ]
        # Sum of all 5 = 500, but max_bundle=2
        result = _rule3_consolidated(
            Decimal("500.00"), invoices,
            Decimal("0.02"), 2   # max_bundle = 2
        )

        # Should not match the sum of all 5
        assert result is None or len(result.get("matched_invoices", [])) <= 2

    def test_no_matching_bundle_returns_none(self):
        """Payment not matching any bundle combination → None."""
        invoices = [
            _make_invoice(300.0, "inv_1"),
            _make_invoice(700.0, "inv_2"),
        ]
        result = _rule3_consolidated(
            Decimal("600.00"), invoices,
            Decimal("0.02"), 5
        )
        assert result is None


# ── Rule 4: Advance payment ────────────────────────────────────────────────────

class TestRule4AdvancePayment:

    def test_advance_returns_advance_status(self):
        """Rule 4 always returns ADVANCE for known retailers."""
        result = _rule4_advance("retailer_001")

        assert result["reconciliation_status"] == "ADVANCE"
        assert result["matched_invoices"] == []
        assert result["confidence"] == 70

    def test_advance_balance_zero(self):
        """Advance payment balance_remaining should be 0."""
        result = _rule4_advance("retailer_002")
        assert result["balance_remaining"] == Decimal("0")


# ── Fallback: Unlinked ─────────────────────────────────────────────────────────

class TestFallback:

    def test_fallback_returns_unlinked(self):
        result = _fallback()
        assert result["reconciliation_status"] == "UNLINKED"
        assert result["confidence"] == 0
        assert result["matched_invoices"] == []

    def test_fallback_rule_field(self):
        result = _fallback()
        assert result["rule"] == "no_match"


# ── Rule ordering tests ────────────────────────────────────────────────────────

class TestRuleOrdering:

    def test_rule1_checked_before_rule2(self):
        """
        A payment exactly matching an invoice should be SETTLED (Rule 1),
        not PARTIAL (Rule 2), even though both would technically match.
        """
        invoices = [_make_invoice(1000.0, "inv_1")]
        payment  = Decimal("1000.00")

        # If we run Rule 1 first (as required), result is SETTLED
        result = (
            _rule1_exact(payment, invoices, Decimal("0.01"))
            or _rule2_partial(payment, invoices)
        )

        assert result["reconciliation_status"] == "SETTLED"

    def test_rule3_only_checked_when_rule1_2_fail(self):
        """
        Consolidated rule should only activate when Rules 1 and 2 fail.
        """
        invoices = [
            _make_invoice(1000.0, "inv_1"),
            _make_invoice(2000.0, "inv_2"),
        ]
        payment = Decimal("3000.00")

        rule1 = _rule1_exact(payment, invoices, Decimal("0.01"))
        rule2 = _rule2_partial(payment, invoices)

        # Rule 1 checks each invoice individually — 3000 != 1000 or 2000
        assert rule1 is None

        # Rule 2 checks if 3000 < any individual invoice — it isn't
        assert rule2 is None

        # Rule 3 should now match (sum of 1000 + 2000)
        rule3 = _rule3_consolidated(payment, invoices, Decimal("0.02"), 5)
        assert rule3 is not None
        assert rule3["reconciliation_status"] == "SETTLED"