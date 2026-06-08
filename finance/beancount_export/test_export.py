"""Tests for the pure Beancount renderer (no DB).

The rendered text is validated by parsing it back with ``beancount.loader``:
a clean load (no errors) proves the output is syntactically valid, balances,
and opens every account before use. Assertions then inspect the parsed
directives rather than matching the printer's exact whitespace.
"""

from __future__ import annotations

from datetime import date

import pytest_bazel
from beancount import loader
from beancount.core import data
from beancount.core.amount import Amount

from finance.augur.budget.schema import BucketDef, BucketKind, TransferDirection
from finance.beancount_export.export import ClassifiedTxn, contra_account, default_account, render_ledger


def _bucket(id: str, kind: BucketKind, direction: TransferDirection, account: str | None = None) -> BucketDef:
    return BucketDef(id=id, label=id.replace("_", " ").title(), kind=kind, direction=direction, account=account)


def _load(text: str) -> list[data.Directive]:
    entries, errors, _ = loader.load_string(text)
    assert not errors, [str(e) for e in errors]
    return entries


def _txns(entries: list[data.Directive]) -> list[data.Transaction]:
    return [e for e in entries if isinstance(e, data.Transaction)]


def _units(txn: data.Transaction, account: str) -> Amount:
    (posting,) = [p for p in txn.postings if p.account == account]
    assert posting.units is not None
    return posting.units


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


def test_output_parses_cleanly() -> None:
    """A clean load proves valid syntax, balanced postings, and opens-before-use."""
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
    entries = _load(render_ledger(txns, buckets, {"acct_checking": "Assets:BofA:Checking"}))
    opens = {e.account for e in entries if isinstance(e, data.Open)}
    assert opens == {"Assets:BofA:Checking", "Expenses:Food:Groceries"}

    (txn,) = _txns(entries)
    assert txn.payee == "Thrive Market"
    assert txn.narration == "THRIVE MARKET"
    assert txn.meta["plaid-id"] == "tx1"
    assert txn.meta["bucket"] == "groceries"
    assert txn.meta["plaid-pfc"] == "FOOD_AND_DRINK / GROCERIES"
    # Expense: contra (Expenses) debited +50, funding (asset) credited -50.
    assert _units(txn, "Expenses:Food:Groceries").number == 50
    assert _units(txn, "Assets:BofA:Checking").number == -50


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
    (txn,) = _txns(_load(render_ledger(txns, buckets, {"acct_checking": "Assets:BofA:Checking"})))
    # Income deposit: asset +1000, income -1000.
    assert _units(txn, "Assets:BofA:Checking").number == 1000
    assert _units(txn, "Income:Salary").number == -1000
    assert txn.payee is None  # no merchant
    assert txn.narration == "OPENAI PAYROLL"


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
    rendered = render_ledger(txns, buckets, funding)
    assert rendered.index("early-a") < rendered.index("early-b") < rendered.index("late")


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
    """A quote in the descriptor survives a render -> parse round-trip."""
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
    (txn,) = _txns(_load(render_ledger(txns, buckets, {"a": "Assets:BofA:Checking"})))
    assert txn.narration == 'ACME "PREMIUM" SVC'


def test_negative_zero_is_normalized() -> None:
    buckets = {"misc": _bucket("misc", BucketKind.EXPENSE, TransferDirection.OUTFLOW)}
    txns = [
        ClassifiedTxn(
            transaction_id="tx", date=date(2026, 5, 1), amount=0.0, name="zero", account_id="a", bucket_id="misc"
        )
    ]
    ledger = render_ledger(txns, buckets, {"a": "Assets:BofA:Checking"})
    assert "-0.00" not in ledger
    _load(ledger)


def test_currency_honors_iso_code() -> None:
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
    (txn,) = _txns(_load(render_ledger(txns, buckets, {"a": "Assets:BofA:Checking"})))
    assert {_units(txn, p.account).currency for p in txn.postings} == {"EUR"}


def test_empty_input_has_header_no_entries() -> None:
    ledger = render_ledger([], {}, {})
    assert 'option "title" "Budget"' in ledger
    assert _txns(_load(ledger)) == []


if __name__ == "__main__":
    raise SystemExit(pytest_bazel.main())
