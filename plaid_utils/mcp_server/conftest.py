"""Fixtures for the Plaid MCP server tests.

`FakePlaidApi` stands in for `plaid_api.PlaidApi`: it reads the SDK request objects via
`.to_dict()` and returns canned responses as plain JSON-able dicts (built from real
`plaid_utils.models`), so the tools' `sanitize_for_serialization` + `model_validate` path
runs end-to-end through an in-memory FastMCP client with no network.
"""

from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastmcp.client import Client

from plaid_utils.mcp_server.config import ResolvedItem
from plaid_utils.mcp_server.server import build_server
from plaid_utils.models import Account, Apr, Balances, CreditLiability, PersonalFinanceCategory, Transaction


class _IdentityApiClient:
    @staticmethod
    def sanitize_for_serialization(obj: Any) -> Any:
        return obj  # FakePlaidApi already returns JSON-able dicts


class FakePlaidApi:
    """In-memory stand-in for plaid_api.PlaidApi: canned responses, no network."""

    def __init__(self, accounts: list[Account], transactions: list[Transaction], credit: list[CreditLiability]) -> None:
        self.api_client = _IdentityApiClient()
        self._accounts = accounts
        self._transactions = transactions
        self._credit = credit

    def _accounts_payload(self) -> list[dict[str, Any]]:
        return [a.model_dump() for a in self._accounts]

    def accounts_get(self, request: Any) -> dict[str, Any]:
        return {"accounts": self._accounts_payload()}

    def accounts_balance_get(self, request: Any) -> dict[str, Any]:
        ids = request.to_dict().get("options", {}).get("account_ids")
        accounts = self._accounts if ids is None else [a for a in self._accounts if a.account_id in ids]
        return {"accounts": [a.model_dump() for a in accounts]}

    def transactions_get(self, request: Any) -> dict[str, Any]:
        options = request.to_dict().get("options", {})
        offset, count = options.get("offset", 0), options.get("count", 50)
        account_ids = options.get("account_ids")
        txns = self._transactions
        if account_ids is not None:
            txns = [t for t in txns if t.account_id in account_ids]
        return {
            "accounts": self._accounts_payload(),
            "transactions": [t.model_dump() for t in txns[offset : offset + count]],
            "total_transactions": len(txns),
        }

    def liabilities_get(self, request: Any) -> dict[str, Any]:
        return {
            "accounts": self._accounts_payload(),
            "liabilities": {"credit": [c.model_dump() for c in self._credit], "mortgage": None, "student": None},
        }


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
def fake_api(
    sample_accounts: list[Account], sample_transactions: list[Transaction], sample_credit: list[CreditLiability]
) -> FakePlaidApi:
    return FakePlaidApi(accounts=sample_accounts, transactions=sample_transactions, credit=sample_credit)


@pytest.fixture
async def client(fake_api: FakePlaidApi, items: dict[str, ResolvedItem]) -> AsyncIterator[Client]:
    async with Client(build_server(fake_api, items)) as connected:
        yield connected
