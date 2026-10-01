from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from datetime import UTC, date, datetime, timedelta
from typing import Any, cast

from finance.plaid.spend.app import _event_stream
from finance.plaid.spend.models import AlertState, CardConfig, CardConfiguration, CardView, SpendView
from finance.plaid.spend.service import SpendService


class _FakeConnection:
    def __init__(
        self, *, accounts: list[dict[str, Any]], liabilities: list[dict[str, Any]], transactions: list[dict[str, Any]]
    ) -> None:
        self.accounts = accounts
        self.liabilities = liabilities
        self.transactions = transactions

    async def fetch(self, query: str, *arguments: Any) -> list[dict[str, Any]]:
        if "FROM public.liability_credit_snapshots AS lc" in query:
            account_ids = arguments[0]
            return [row for row in self.liabilities if row["account_id"] in account_ids]
        if "FROM public.transactions AS t" in query:
            account_ids = arguments[0]
            return [row for row in self.transactions if row["account_id"] in account_ids]
        if "FROM public.accounts AS a" in query:
            account_ids = arguments[0]
            return [row for row in self.accounts if row["account_id"] in account_ids]
        raise AssertionError(f"unexpected spend query: {query}")


class _ConnectionContext:
    def __init__(self, connection: _FakeConnection) -> None:
        self.connection = connection

    async def __aenter__(self) -> _FakeConnection:
        return self.connection

    async def __aexit__(self, *_: object) -> None:
        return None


class _FakePool:
    def __init__(self, connection: _FakeConnection) -> None:
        self.connection = connection

    def acquire(self) -> _ConnectionContext:
        return _ConnectionContext(self.connection)


def _transaction(
    account_id: str,
    transaction_id: str,
    transaction_date: date,
    amount: float,
    *,
    pending: bool = False,
    pending_transaction_id: str | None = None,
    category: str | None = None,
    currency: str = "USD",
) -> dict[str, Any]:
    return {
        "account_id": account_id,
        "transaction_id": transaction_id,
        "date": transaction_date,
        "amount": amount,
        "pending": pending,
        "pending_transaction_id": pending_transaction_id,
        "currency": currency,
        "pfc_detailed": category,
    }


async def test_read_view_uses_statement_cycle_and_normalizes_transactions() -> None:
    today = datetime.now(UTC).date()
    early_cycle_start = today - timedelta(days=10)
    late_cycle_start = today - timedelta(days=3)
    account_ids = ("card-early", "card-late")
    accounts = [
        {
            "account_id": account_id,
            "account_name": f"{account_id} account",
            "mask": "1234",
            "currency": "USD",
            "institution_name": "Example Bank",
            "item_id": "item-1",
            "last_synced_at": datetime(2026, 9, 1, tzinfo=UTC),
        }
        for account_id in account_ids
    ]
    liabilities = [
        {"account_id": "card-early", "last_statement_issue_date": (early_cycle_start - timedelta(days=1)).isoformat()},
        {"account_id": "card-late", "last_statement_issue_date": (late_cycle_start - timedelta(days=1)).isoformat()},
    ]
    transactions = [
        # The SQL query uses the earliest selected cycle. Python must still apply this card's later boundary.
        _transaction("card-late", "before-cycle", late_cycle_start - timedelta(days=1), 90.00),
        _transaction("card-late", "cycle-start", late_cycle_start, 12.34),
        _transaction("card-late", "refund", today, -3.00),
        _transaction("card-late", "pending-only", today, 7.50, pending=True),
        _transaction("card-late", "pending-old", today, 20.00, pending=True),
        _transaction("card-late", "posted-replacement", today, 22.00, pending_transaction_id="pending-old"),
        _transaction("card-late", "card-payment", today, 100.00, category="LOAN_PAYMENTS_CREDIT_CARD_PAYMENT"),
        _transaction("card-late", "other-currency", today, 40.00, currency="CAD"),
        _transaction("card-late", "future", today + timedelta(days=1), 60.00),
    ]
    connection = _FakeConnection(accounts=accounts, liabilities=liabilities, transactions=transactions)
    configuration = CardConfiguration(
        cards=[
            CardConfig(account_id=account_id, label=account_id, limit_minor_units=100_000, enabled=True)
            for account_id in account_ids
        ]
    )
    service = SpendService("unused", configuration)
    service._pool = cast(Any, _FakePool(connection))

    view = await service.read_view()

    early_card, late_card = view.cards
    assert early_card.cycle_start == early_cycle_start
    assert early_card.spend_minor_units == 0
    assert late_card.cycle_start == late_cycle_start
    assert late_card.posted_minor_units == 3_134  # 12.34 + refund (-3.00) + posted replacement (22.00)
    assert late_card.pending_minor_units == 750  # The superseded $20 pending row is not double-counted.
    assert late_card.spend_minor_units == 3_884


class _ConnectedRequest:
    async def is_disconnected(self) -> bool:
        return False


class _EventService(SpendService):
    def __init__(self, views: list[SpendView]) -> None:
        super().__init__("unused", CardConfiguration())
        self.views = iter(views)
        self.listening.set()

    async def read_view(self) -> SpendView:
        return next(self.views)


def _view(amount_minor_units: int) -> SpendView:
    return SpendView(
        generated_at=datetime(2026, 9, 30, tzinfo=UTC),
        cards=[
            CardView(
                account_id="card-1",
                label="Main card",
                account_name="Main card account",
                institution_name="Example Bank",
                mask="1234",
                currency="USD",
                cycle_start=date(2026, 9, 1),
                spend_minor_units=amount_minor_units,
                posted_minor_units=amount_minor_units,
                pending_minor_units=0,
                limit_minor_units=100_000,
                alert_threshold_percent=80,
                spend_percent=amount_minor_units / 1000,
                alert_state=AlertState.NORMAL,
                last_synced_at=None,
                statement_available=True,
            )
        ],
    )


async def test_event_stream_sends_initial_view_and_notification_update() -> None:
    service = _EventService([_view(1_000), _view(2_500)])
    stream: AsyncGenerator[str] = _event_stream(cast(Any, _ConnectedRequest()), service)

    initial_event = await anext(stream)
    initial_payload = json.loads(initial_event.partition("data: ")[2])
    assert initial_payload["cards"][0]["spend_minor_units"] == 1_000

    service._on_notification(None, 0, "plaid_spend_changed", None)
    updated_event = await anext(stream)
    updated_payload = json.loads(updated_event.partition("data: ")[2])
    assert updated_payload["cards"][0]["spend_minor_units"] == 2_500

    await stream.aclose()
    assert not service._subscribers


if __name__ == "__main__":
    import pytest_bazel

    pytest_bazel.main()
