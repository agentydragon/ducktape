"""Spend-service integration tests against a migrated PostgreSQL testcontainer."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncGenerator
from datetime import UTC, date, datetime, timedelta
from typing import Any, cast

import asyncpg
import pytest
import pytest_asyncio
from sqlalchemy.engine import make_url
from testcontainers.postgres import PostgresContainer

from finance.plaid.db.link_store import PlaidLinkStorage
from finance.plaid.spend.allowance import AllowancePolicy, CategoryExact, Kind, Rule, Status
from finance.plaid.spend.app import _event_stream
from finance.plaid.spend.models import AlertState, CardConfig, SpendConfiguration
from finance.plaid.spend.service import SpendService
from util.testing.postgres import create_database_async, force_drop_database
from util.testing.postgres_fixtures import postgres_container  # noqa: F401


@pytest.fixture(scope="session")
def postgres_admin_url(postgres_container: PostgresContainer) -> str:  # noqa: F811
    host = postgres_container.get_container_host_ip()
    port = int(postgres_container.get_exposed_port(5432))
    return f"postgresql+asyncpg://postgres:postgres@{host}:{port}/postgres"


@pytest_asyncio.fixture
async def postgres_url(postgres_admin_url: str, request: pytest.FixtureRequest) -> AsyncGenerator[str]:
    db_name = re.sub(r"[^a-z0-9]", "_", request.node.name.lower())[:45].rstrip("_") or "spend_test"
    db_url = await create_database_async(postgres_admin_url, db_name)
    storage = await PlaidLinkStorage.initialize(db_url)
    try:
        yield make_url(db_url).set(drivername="postgresql").render_as_string(hide_password=False)
    finally:
        await storage.close()
        await force_drop_database(postgres_admin_url, db_name)


@pytest_asyncio.fixture
async def connection(postgres_url: str) -> AsyncGenerator[asyncpg.Connection]:
    conn = await asyncpg.connect(postgres_url)
    try:
        yield conn
    finally:
        await conn.close()


async def add_link(connection: asyncpg.Connection, item_id: str, *, synced: datetime) -> None:
    await connection.execute(
        """INSERT INTO public.links
           (item_id, institution_name, products_requested, products_authorized, products_billed,
            status, access_token_secret, last_synced_at, created_at, updated_at)
           VALUES ($1, 'Example Bank', '[]'::json, '[]'::json, '[]'::json, 'active', 'example-secret', $2, $2, $2)""",
        item_id,
        synced,
    )


async def add_account(connection: asyncpg.Connection, account_id: str, item_id: str, *, type: str) -> None:
    await connection.execute(
        """INSERT INTO public.accounts (account_id, item_id, name, mask, type, iso_currency_code, raw_json, updated_at)
           VALUES ($1, $2, $3, '1234', $4, 'USD', '{}'::json, NOW())""",
        account_id,
        item_id,
        f"Example {account_id}",
        type,
    )


async def add_liability(connection: asyncpg.Connection, account_id: str, item_id: str, issue: date) -> None:
    await connection.execute(
        """INSERT INTO public.liability_credit_snapshots (account_id, item_id, captured_at, raw_json)
           VALUES ($1, $2, $3, $4::json)""",
        account_id,
        item_id,
        datetime.now(UTC),
        json.dumps({"last_statement_issue_date": issue.isoformat()}),
    )


async def add_transaction(
    connection: asyncpg.Connection,
    account_id: str,
    item_id: str,
    transaction_id: str,
    on_date: date,
    amount: float,
    *,
    pending: bool = False,
    pending_transaction_id: str | None = None,
    category: str | None = None,
    currency: str = "USD",
) -> None:
    await connection.execute(
        """INSERT INTO public.transactions
           (transaction_id, account_id, item_id, date, amount, name, merchant_name, pending,
            pending_transaction_id, pfc_primary, pfc_detailed, iso_currency_code, removed, raw_json, updated_at)
           VALUES ($1, $2, $3, $4, $5, 'EXAMPLE SHOP', NULL, $6, $7, 'SHOPPING', $8, $9, false, '{}'::json, NOW())""",
        transaction_id,
        account_id,
        item_id,
        on_date,
        amount,
        pending,
        pending_transaction_id,
        category,
        currency,
    )


async def test_read_view_uses_statement_cycle_and_normalizes_transactions(
    connection: asyncpg.Connection, postgres_url: str
) -> None:
    today = datetime.now(UTC).date()
    early_cycle_start = today - timedelta(days=10)
    late_cycle_start = today - timedelta(days=3)
    await add_link(connection, "item-1", synced=datetime.now(UTC))
    for account_id, start in (("card-early", early_cycle_start), ("card-late", late_cycle_start)):
        await add_account(connection, account_id, "item-1", type="credit")
        await add_liability(connection, account_id, "item-1", start - timedelta(days=1))
    # The SQL query uses the earliest selected cycle. Python still applies the later card boundary.
    for transaction_id, on_date, amount in (
        ("before-cycle", late_cycle_start - timedelta(days=1), 90.0),
        ("cycle-start", late_cycle_start, 12.34),
        ("refund", today, -3.0),
        ("pending-only", today, 7.5),
        ("pending-old", today, 20.0),
        ("posted-replacement", today, 22.0),
        ("card-payment", today, 100.0),
        ("other-currency", today, 40.0),
        ("future", today + timedelta(days=1), 60.0),
    ):
        await add_transaction(
            connection,
            "card-late",
            "item-1",
            transaction_id,
            on_date,
            amount,
            pending=transaction_id in ("pending-only", "pending-old"),
            pending_transaction_id="pending-old" if transaction_id == "posted-replacement" else None,
            category="LOAN_PAYMENTS_CREDIT_CARD_PAYMENT" if transaction_id == "card-payment" else None,
            currency="CAD" if transaction_id == "other-currency" else "USD",
        )
    configuration = SpendConfiguration(
        cards=[
            CardConfig(account_id=aid, label=aid, limit_minor_units=100_000, enabled=True)
            for aid in ("card-early", "card-late")
        ]
    )
    service = SpendService(postgres_url, configuration, dashboard_url="https://spend.example.test")
    await service.start()
    try:
        view = await service.read_view()
    finally:
        await service.close()
    early_card, late_card = view.cards
    assert early_card.cycle_start == early_cycle_start
    assert early_card.spend_minor_units == 0
    assert late_card.cycle_start == late_cycle_start
    assert late_card.posted_minor_units == 3_134
    assert late_card.pending_minor_units == 750
    assert late_card.spend_minor_units == 3_884


async def test_card_without_statement_reports_observed_spend_not_a_statement_cycle(
    connection: asyncpg.Connection, postgres_url: str
) -> None:
    today = datetime.now(UTC).date()
    start = today - timedelta(days=32)
    await add_link(connection, "item-new", synced=datetime.now(UTC))
    for account_id in ("card-first", "card-empty"):
        await add_account(connection, account_id, "item-new", type="credit")
    await add_transaction(connection, "card-first", "item-new", "first", start, 10.0)
    await add_transaction(connection, "card-first", "item-new", "pending", today, 15.0, pending=True)
    await add_transaction(connection, "card-first", "item-new", "posted", today, 17.0, pending_transaction_id="pending")
    await add_transaction(connection, "card-first", "item-new", "new", today, 12.0)
    await add_transaction(
        connection, "card-first", "item-new", "repayment", today, 30.0, category="LOAN_PAYMENTS_CREDIT_CARD_PAYMENT"
    )
    service = SpendService(
        postgres_url,
        SpendConfiguration(
            cards=[
                CardConfig(account_id=aid, label=aid, limit_minor_units=10_000, enabled=True)
                for aid in ("card-first", "card-empty")
            ]
        ),
        dashboard_url="https://spend.example.test",
    )
    await service.start()
    try:
        view = await service.read_view()
    finally:
        await service.close()
    cards = {card.account_id: card for card in view.cards}
    first, empty = cards["card-first"], cards["card-empty"]
    assert first.cycle_start == start  # first observed transaction, not a statement boundary
    assert first.statement_available is False
    assert first.posted_minor_units == 3_900
    assert first.pending_minor_units == 0  # superseded by posted
    assert first.spend_minor_units == 3_900
    assert first.spend_percent is None
    assert first.alert_state == AlertState.UNAVAILABLE
    assert empty.cycle_start is None
    assert empty.spend_minor_units is None


async def test_allowance_account_coverage_and_freshness_gate(connection: asyncpg.Connection, postgres_url: str) -> None:
    now = datetime.now(UTC)
    midnight = datetime.combine(now.date(), datetime.min.time(), tzinfo=UTC)
    for account_id, item_id, type in (("card-1", "item-card", "credit"), ("checking-1", "item-checking", "depository")):
        await add_link(connection, item_id, synced=now)
        await add_account(connection, account_id, item_id, type=type)
    await add_transaction(connection, "card-1", "item-card", "purchase", now.date(), 12.0)
    config = SpendConfiguration(
        cards=[],
        allowance=AllowancePolicy(
            monthly_minor_units=10_000,
            activation_at=midnight.date(),
            spending_account_ids={"card-1", "checking-1"},
            rules=[
                Rule(
                    condition=CategoryExact(type="category_exact", field="pfc_primary", value="SHOPPING"),
                    kind=Kind.FLEXIBLE,
                )
            ],
        ),
    )
    service = SpendService(postgres_url, config, dashboard_url="https://spend.example.test")
    await service.start()
    try:
        result = await service.read_view()
        assert result.allowance is not None
        assert result.allowance.available_minor_units == 8_800
        await connection.execute("UPDATE public.links SET status = 'removed' WHERE item_id = 'item-checking'")
        unavailable = (await service.read_view()).allowance
        assert unavailable is not None
        assert unavailable.status == Status.UNAVAILABLE
        await connection.execute(
            "UPDATE public.links SET status = 'active', last_synced_at = $1 WHERE item_id = 'item-checking'",
            now - timedelta(days=4),
        )
        unavailable = (await service.read_view()).allowance
        assert unavailable is not None
        assert unavailable.status == Status.UNAVAILABLE
    finally:
        await service.close()


class _ConnectedRequest:
    async def is_disconnected(self) -> bool:
        return False


async def test_event_stream_sends_initial_view_and_notification_update(
    connection: asyncpg.Connection, postgres_url: str
) -> None:
    today = datetime.now(UTC).date()
    await add_link(connection, "item-1", synced=datetime.now(UTC))
    await add_account(connection, "card-1", "item-1", type="credit")
    await add_liability(connection, "card-1", "item-1", today - timedelta(days=1))
    await add_transaction(connection, "card-1", "item-1", "purchase", today, 10.0)
    service = SpendService(
        postgres_url,
        SpendConfiguration(cards=[CardConfig(account_id="card-1", label="Test", enabled=True)]),
        dashboard_url="https://spend.example.test",
    )
    await service.start()
    stream: AsyncGenerator[str] | None = None
    try:
        await asyncio.wait_for(service.listening.wait(), timeout=10)
        stream = _event_stream(cast(Any, _ConnectedRequest()), service)
        initial = json.loads((await asyncio.wait_for(anext(stream), timeout=10)).partition("data: ")[2])
        assert initial["cards"][0]["spend_minor_units"] == 1_000
        await connection.execute("UPDATE public.transactions SET amount = 25 WHERE transaction_id = 'purchase'")
        await connection.execute("SELECT pg_notify('plaid_spend_changed', '')")
        updated = json.loads((await asyncio.wait_for(anext(stream), timeout=10)).partition("data: ")[2])
        assert updated["cards"][0]["spend_minor_units"] == 2_500
    finally:
        if stream is not None:
            await stream.aclose()
        await service.close()
    assert not service._subscribers


if __name__ == "__main__":
    import pytest_bazel

    pytest_bazel.main()
