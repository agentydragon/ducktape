"""Tests for the pure Beancount renderer (no DB)."""

from __future__ import annotations

from datetime import date

import pytest_bazel

from augur.budget.export import ClassifiedTxn, contra_account, default_account, render_ledger
from augur.budget.schema import BucketDef, BucketKind, TransferDirection


def _bucket(id: str, kind: BucketKind, direction: TransferDirection, account: str | None = None) -> BucketDef:
    return BucketDef(id=id, label=id.replace("_", " ").title(), kind=kind, direction=direction, account=account)


def test_default_account_derives_capwords_path() -> None:
    bucket = _bucket("bay_area_psychiatric", BucketKind.EXPENSE, TransferDirection.OUTFLOW)
    assert default_account(bucket) == "Expenses:BayAreaPsychiatric"


def test_default_account_per_kind_root() -> None:
    assert default_account(_bucket("salary", BucketKind.INCOME, TransferDirection.INFLOW)) == "Income:Salary"
    assert default_account(_bucket("refund", BucketKind.INFLOW, TransferDirection.INFLOW)) == "Income:Refund"
    assert (
        default_account(_bucket("brokerage", BucketKind.TRANSFER, TransferDirection.OUTFLOW))
        == "Equity:Transfers:Brokerage"
    )


def test_contra_prefers_explicit_account() -> None:
    bucket = _bucket("thrive_market", BucketKind.EXPENSE, TransferDirection.OUTFLOW, "Expenses:Food:Groceries:Thrive")
    assert contra_account(bucket) == "Expenses:Food:Groceries:Thrive"


def test_expense_signs_and_postings() -> None:
    buckets = {
        "groceries": _bucket("groceries", BucketKind.EXPENSE, TransferDirection.OUTFLOW, "Expenses:Food:Groceries")
    }
    txns = [
        ClassifiedTxn(
            transaction_id="tx1",
            date=date(2026, 5, 27),
            amount=50.0,
            name="THRIVE MARKET",
            account_id="acct_checking",
            bucket_id="groceries",
            merchant_name="Thrive Market",
            pfc_primary="FOOD_AND_DRINK",
            pfc_detailed="GROCERIES",
        )
    ]
    ledger = render_ledger(txns, buckets, {"acct_checking": "Assets:BofA:Checking"})

    assert 'option "operating_currency" "USD"' in ledger
    assert "2026-05-27 open Assets:BofA:Checking" in ledger
    assert "2026-05-27 open Expenses:Food:Groceries" in ledger
    assert '2026-05-27 * "Thrive Market" "THRIVE MARKET"' in ledger
    assert '  plaid-id: "tx1"' in ledger
    assert '  bucket: "groceries"' in ledger
    assert '  plaid-pfc: "FOOD_AND_DRINK / GROCERIES"' in ledger
    # Expense: contra (Expenses) debited +50, funding (asset) credited -50.
    assert "  Expenses:Food:Groceries  50.00 USD" in ledger
    assert "  Assets:BofA:Checking  -50.00 USD" in ledger


def test_income_signs_are_inverted() -> None:
    buckets = {"salary": _bucket("salary", BucketKind.INCOME, TransferDirection.INFLOW, "Income:Salary")}
    txns = [
        ClassifiedTxn(
            transaction_id="tx_pay",
            date=date(2026, 5, 1),
            amount=-1000.0,  # Plaid signs inflows negative.
            name="OPENAI PAYROLL",
            account_id="acct_checking",
            bucket_id="salary",
        )
    ]
    ledger = render_ledger(txns, buckets, {"acct_checking": "Assets:BofA:Checking"})
    # Income deposit: asset +1000, income -1000.
    assert "  Assets:BofA:Checking  1000.00 USD" in ledger
    assert "  Income:Salary  -1000.00 USD" in ledger
    # No merchant -> narration-only header.
    assert '2026-05-01 * "OPENAI PAYROLL"' in ledger


def test_entries_ordered_by_date_then_id() -> None:
    buckets = {"misc": _bucket("misc", BucketKind.EXPENSE, TransferDirection.OUTFLOW)}
    funding = {"a": "Assets:BofA:Checking"}
    txns = [
        ClassifiedTxn(
            transaction_id="z", date=date(2026, 5, 2), amount=1.0, name="late", account_id="a", bucket_id="misc"
        ),
        ClassifiedTxn(
            transaction_id="b", date=date(2026, 5, 1), amount=1.0, name="early-b", account_id="a", bucket_id="misc"
        ),
        ClassifiedTxn(
            transaction_id="a", date=date(2026, 5, 1), amount=1.0, name="early-a", account_id="a", bucket_id="misc"
        ),
    ]
    ledger = render_ledger(txns, buckets, funding)
    assert ledger.index("early-a") < ledger.index("early-b") < ledger.index("late")


def test_render_is_deterministic() -> None:
    buckets = {"misc": _bucket("misc", BucketKind.EXPENSE, TransferDirection.OUTFLOW)}
    funding = {"a": "Assets:BofA:Checking"}
    txns = [
        ClassifiedTxn(
            transaction_id="b", date=date(2026, 5, 1), amount=2.5, name="two", account_id="a", bucket_id="misc"
        ),
        ClassifiedTxn(
            transaction_id="a", date=date(2026, 5, 1), amount=1.0, name="one", account_id="a", bucket_id="misc"
        ),
    ]
    assert render_ledger(txns, buckets, funding) == render_ledger(list(reversed(txns)), buckets, funding)


def test_string_escaping_in_narration() -> None:
    buckets = {"misc": _bucket("misc", BucketKind.EXPENSE, TransferDirection.OUTFLOW)}
    txns = [
        ClassifiedTxn(
            transaction_id="tx",
            date=date(2026, 5, 1),
            amount=1.0,
            name='ACME "PREMIUM" SVC',
            account_id="a",
            bucket_id="misc",
        )
    ]
    ledger = render_ledger(txns, buckets, {"a": "Assets:BofA:Checking"})
    assert '"ACME \\"PREMIUM\\" SVC"' in ledger


def test_negative_zero_is_normalized() -> None:
    buckets = {"misc": _bucket("misc", BucketKind.EXPENSE, TransferDirection.OUTFLOW)}
    txns = [
        ClassifiedTxn(
            transaction_id="tx", date=date(2026, 5, 1), amount=0.0, name="zero", account_id="a", bucket_id="misc"
        )
    ]
    ledger = render_ledger(txns, buckets, {"a": "Assets:BofA:Checking"})
    assert "-0.00" not in ledger


def test_currency_falls_back_then_honors_iso_code() -> None:
    buckets = {"misc": _bucket("misc", BucketKind.EXPENSE, TransferDirection.OUTFLOW)}
    txns = [
        ClassifiedTxn(
            transaction_id="tx",
            date=date(2026, 5, 1),
            amount=1.0,
            name="euro",
            account_id="a",
            bucket_id="misc",
            iso_currency_code="EUR",
        )
    ]
    ledger = render_ledger(txns, buckets, {"a": "Assets:BofA:Checking"})
    assert "1.00 EUR" in ledger


def test_empty_input_has_header_no_entries() -> None:
    ledger = render_ledger([], {}, {})
    assert 'option "title" "Budget"' in ledger
    assert " open " not in ledger
    assert ledger.endswith("\n")


if __name__ == "__main__":
    raise SystemExit(pytest_bazel.main())
