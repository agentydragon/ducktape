"""Unit tests for the typed-Plaid -> compact-output mappers."""

import pytest_bazel

from plaid.mcp_server.projections import account_out, card_liability_out, transaction_out
from plaid.models import Account, Apr, Balances, CreditLiability, PersonalFinanceCategory, Transaction


def test_account_out_maps_balances_and_nullables() -> None:
    account = Account(
        account_id="x",
        name="Checking",
        official_name=None,
        mask="1199",
        type="depository",
        subtype="checking",
        balances=Balances(available=10.0, current=12.0, limit=None, iso_currency_code="USD"),
    )
    out = account_out(account)
    assert out.account_id == "x"
    assert out.official_name is None
    assert out.balances.current == 12.0
    assert out.balances.limit is None


def test_transaction_out_without_category() -> None:
    txn = Transaction(
        transaction_id="t",
        account_id="a",
        date="2026-05-01",
        amount=5.0,
        iso_currency_code="USD",
        name="X",
        merchant_name=None,
        pending=True,
        pending_transaction_id=None,
        personal_finance_category=None,
    )
    out = transaction_out(txn)
    assert out.category is None
    assert out.pending is True


def test_transaction_out_with_category() -> None:
    txn = Transaction(
        transaction_id="t",
        account_id="a",
        date="2026-05-01",
        amount=5.0,
        iso_currency_code="USD",
        name="X",
        merchant_name="Merchant",
        pending=False,
        pending_transaction_id="p",
        personal_finance_category=PersonalFinanceCategory(
            primary="FOOD_AND_DRINK", detailed="FOOD_AND_DRINK_GROCERIES"
        ),
    )
    out = transaction_out(txn)
    assert out.category is not None
    assert out.category.primary == "FOOD_AND_DRINK"
    assert out.pending_transaction_id == "p"


def test_card_liability_out_joins_account() -> None:
    account = Account(
        account_id="cc",
        name="Sapphire",
        official_name=None,
        mask="4021",
        type="credit",
        subtype="credit card",
        balances=Balances(available=None, current=100.0, limit=1000.0, iso_currency_code="USD"),
    )
    credit = CreditLiability(
        account_id="cc", aprs=[Apr(apr_type="purchase_apr", apr_percentage=20.0)], last_statement_balance=100.0
    )
    out = card_liability_out(credit, {"cc": account})
    assert out.name == "Sapphire"
    assert out.mask == "4021"
    assert out.aprs[0].type == "purchase_apr"
    assert out.aprs[0].balance_subject_to_apr is None


def test_card_liability_out_missing_account_falls_back() -> None:
    out = card_liability_out(CreditLiability(account_id="ghost"), {})
    assert out.name == "ghost"
    assert out.mask is None
    assert out.aprs == []


if __name__ == "__main__":
    pytest_bazel.main()
