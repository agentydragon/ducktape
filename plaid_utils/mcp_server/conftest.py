"""Fixtures for the Plaid MCP server tests.

`FakePlaidExtras` returns canned typed responses (real `plaid.models` instances —
no mocking), so tests exercise the real tool registration, schemas, and handlers
through an in-memory FastMCP client without any network.
"""

from collections.abc import AsyncIterator
from datetime import date

import pytest
from fastmcp.client import Client

from plaid_utils.client import PlaidExtras
from plaid_utils.mcp_server.config import ResolvedItem
from plaid_utils.mcp_server.server import build_server
from plaid_utils.models import (
    Account,
    AccountsGetResponse,
    Apr,
    Balances,
    CreditLiability,
    Liabilities,
    LiabilitiesGetResponse,
    PersonalFinanceCategory,
    Transaction,
    TransactionsGetResponse,
)


class FakePlaidExtras(PlaidExtras):
    """In-memory PlaidExtras: canned typed responses, no creds/network."""

    def __init__(self, accounts: list[Account], transactions: list[Transaction], credit: list[CreditLiability]) -> None:
        self._accounts = accounts
        self._transactions = transactions
        self._credit = credit

    def accounts_get(self, access_token: str) -> AccountsGetResponse:
        return AccountsGetResponse(accounts=self._accounts)

    def accounts_balance_get(self, access_token: str, account_ids: list[str] | None = None) -> AccountsGetResponse:
        accounts = self._accounts if account_ids is None else [a for a in self._accounts if a.account_id in account_ids]
        return AccountsGetResponse(accounts=accounts)

    def transactions_get(
        self,
        access_token: str,
        start_date: date,
        end_date: date,
        account_ids: list[str] | None = None,
        offset: int = 0,
        count: int = 50,
    ) -> TransactionsGetResponse:
        txns = self._transactions
        if account_ids is not None:
            txns = [t for t in txns if t.account_id in account_ids]
        return TransactionsGetResponse(
            accounts=self._accounts, transactions=txns[offset : offset + count], total_transactions=len(txns)
        )

    def liabilities_get(self, access_token: str) -> LiabilitiesGetResponse:
        return LiabilitiesGetResponse(accounts=self._accounts, liabilities=Liabilities(credit=self._credit))


@pytest.fixture
def sample_accounts() -> list[Account]:
    return [
        Account(
            account_id="acc_cc",
            name="Sapphire Reserve",
            official_name="Chase Sapphire Reserve",
            mask="4021",
            type="credit",
            subtype="credit card",
            balances=Balances(available=8456.79, current=1543.21, limit=10000.0, iso_currency_code="USD"),
        ),
        Account(
            account_id="acc_chk",
            name="Checking",
            official_name=None,
            mask="1199",
            type="depository",
            subtype="checking",
            balances=Balances(available=2500.0, current=2500.0, limit=None, iso_currency_code="USD"),
        ),
    ]


@pytest.fixture
def sample_transactions() -> list[Transaction]:
    return [
        Transaction(
            transaction_id=f"txn_{i}",
            account_id="acc_cc",
            date="2026-05-20",
            amount=10.0 + i,
            iso_currency_code="USD",
            name=f"MERCHANT {i}",
            merchant_name=f"Merchant {i}",
            pending=False,
            pending_transaction_id=None,
            personal_finance_category=PersonalFinanceCategory(
                primary="FOOD_AND_DRINK", detailed="FOOD_AND_DRINK_GROCERIES"
            ),
        )
        for i in range(5)
    ]


@pytest.fixture
def sample_credit() -> list[CreditLiability]:
    return [
        CreditLiability(
            account_id="acc_cc",
            aprs=[
                Apr(
                    apr_type="purchase_apr",
                    apr_percentage=22.49,
                    balance_subject_to_apr=1543.21,
                    interest_charge_amount=0.0,
                )
            ],
            is_overdue=False,
            last_statement_balance=1543.21,
            last_statement_issue_date="2026-05-05",
            minimum_payment_amount=40.0,
            next_payment_due_date="2026-06-01",
            last_payment_amount=1200.0,
            last_payment_date="2026-04-29",
        )
    ]


@pytest.fixture
def items() -> dict[str, ResolvedItem]:
    return {
        "chase": ResolvedItem(
            key="chase", institution="Chase", products=["transactions", "liabilities"], access_token="tok-chase"
        ),
        "bofa": ResolvedItem(
            key="bofa", institution="Bank of America", products=["transactions"], access_token="tok-bofa"
        ),
    }


@pytest.fixture
def fake_extras(
    sample_accounts: list[Account], sample_transactions: list[Transaction], sample_credit: list[CreditLiability]
) -> FakePlaidExtras:
    return FakePlaidExtras(accounts=sample_accounts, transactions=sample_transactions, credit=sample_credit)


@pytest.fixture
async def client(fake_extras: FakePlaidExtras, items: dict[str, ResolvedItem]) -> AsyncIterator[Client]:
    async with Client(build_server(fake_extras, items)) as connected:
        yield connected
