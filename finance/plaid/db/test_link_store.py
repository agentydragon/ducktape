from __future__ import annotations

import re
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_bazel
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from testcontainers.postgres import PostgresContainer

from finance.plaid.db.link_store import ApiEvent, PlaidLinkStorage, SyncAlreadyRunningError
from util.testing.postgres import force_drop_database
from util.testing.postgres_fixtures import postgres_container  # noqa: F401


@pytest.fixture(scope="session")
def postgres_admin_url(postgres_container: PostgresContainer) -> str:  # noqa: F811
    host = postgres_container.get_container_host_ip()
    port = int(postgres_container.get_exposed_port(5432))
    return f"postgresql+asyncpg://postgres:postgres@{host}:{port}/postgres"


@pytest.fixture
async def db_url(postgres_admin_url: str, request: pytest.FixtureRequest) -> AsyncGenerator[str]:
    db_name = re.sub(r"[^a-z0-9]", "_", request.node.name.lower())[:45].rstrip("_") or "plaid_test"
    admin_engine = create_async_engine(postgres_admin_url, isolation_level="AUTOCOMMIT")
    async with admin_engine.connect() as conn:
        await conn.execute(text(f'CREATE DATABASE "{db_name}"'))
    await admin_engine.dispose()
    try:
        yield make_url(postgres_admin_url).set(database=db_name).render_as_string(hide_password=False)
    finally:
        await force_drop_database(postgres_admin_url, db_name)


@pytest.fixture
async def storage(db_url: str) -> AsyncGenerator[PlaidLinkStorage]:
    store = await PlaidLinkStorage.initialize(db_url)
    try:
        yield store
    finally:
        await store.close()


async def _add_link(storage: PlaidLinkStorage, *, item_id: str = "item-investments") -> None:
    await storage.upsert_link(
        item_id=item_id,
        access_token_secret=f"{item_id}-token",
        products_requested=["investments"],
        institution_id="ins_investments",
        institution_name="Investment Test",
        label=None,
    )


async def test_successful_sync_run_marks_investment_only_link_synced(storage: PlaidLinkStorage) -> None:
    await _add_link(storage)
    before = datetime.now(UTC)

    run_id = await storage.begin_sync_run(trigger="link", item_id="item-investments", configured_windows={})
    await storage.finish_sync_run(run_id, status="succeeded")

    link = await storage.get_link("item-investments")
    assert link is not None
    assert link.last_synced_at is not None
    assert link.last_synced_at >= before


async def test_failed_sync_run_does_not_mark_link_synced(storage: PlaidLinkStorage) -> None:
    await _add_link(storage)

    run_id = await storage.begin_sync_run(trigger="link", item_id="item-investments", configured_windows={})
    await storage.finish_sync_run(run_id, status="failed", error_summary="boom")

    link = await storage.get_link("item-investments")
    assert link is not None
    assert link.last_synced_at is None


async def test_transaction_delta_commits_changes_and_cursor_together(storage: PlaidLinkStorage, db_url: str) -> None:
    await storage.upsert_link(
        item_id="item-transactions",
        access_token_secret="item-transactions-token",
        products_requested=["transactions"],
        institution_id="ins_transactions",
        institution_name="Transactions Test",
        label=None,
    )

    await storage.apply_accounts(
        item_id="item-transactions",
        accounts=[{"account_id": "account-transactions", "name": "Checking", "type": "depository", "balances": {}}],
        captured_at=datetime(2026, 5, 31, 12, 0, tzinfo=UTC),
    )
    captured_at = datetime(2026, 5, 31, 12, 0, tzinfo=UTC)
    await storage.apply_transaction_delta(
        item_id="item-transactions",
        added=[
            {
                "transaction_id": "txn-kept",
                "account_id": "account-transactions",
                "date": "2026-05-30",
                "amount": 12.34,
                "name": "Coffee",
                "pending": False,
            },
            {
                "transaction_id": "txn-removed",
                "account_id": "account-transactions",
                "date": "2026-05-30",
                "amount": 9.0,
                "name": "Old charge",
                "pending": False,
            },
        ],
        modified=[],
        removed=[],
        next_cursor="cursor-first",
        captured_at=captured_at,
    )
    await storage.apply_transaction_delta(
        item_id="item-transactions",
        added=[],
        modified=[
            {
                "transaction_id": "txn-kept",
                "account_id": "account-transactions",
                "date": "2026-05-30",
                "amount": 10.0,
                "name": "Updated Coffee",
                "pending": False,
            }
        ],
        removed=[{"transaction_id": "txn-removed"}],
        next_cursor="cursor-second",
        captured_at=captured_at,
    )

    link = await storage.get_link("item-transactions")
    assert link is not None
    assert link.last_synced_at is None
    assert link.transactions_cursor == "cursor-second"
    engine = create_async_engine(db_url)
    try:
        async with engine.connect() as conn:
            kept = (
                await conn.execute(
                    text("SELECT name, amount, removed FROM transactions WHERE transaction_id = 'txn-kept'")
                )
            ).one()
            removed = (
                await conn.execute(text("SELECT removed FROM transactions WHERE transaction_id = 'txn-removed'"))
            ).scalar_one()
        assert kept == ("Updated Coffee", 10.0, False)
        assert removed is True
    finally:
        await engine.dispose()


async def test_sync_run_claim_is_unique_per_item(storage: PlaidLinkStorage) -> None:
    await _add_link(storage)
    await storage.begin_sync_run(trigger="cron", item_id="item-investments", configured_windows={})

    with pytest.raises(SyncAlreadyRunningError):
        await storage.begin_sync_run(trigger="webhook", item_id="item-investments", configured_windows={})


async def test_sync_run_claim_recovers_a_stale_run(storage: PlaidLinkStorage, db_url: str) -> None:
    await _add_link(storage)
    stale_run_id = await storage.begin_sync_run(trigger="cron", item_id="item-investments", configured_windows={})
    engine = create_async_engine(db_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE sync_runs SET started_at = :started_at WHERE run_id = :run_id"),
                {"started_at": datetime.now(UTC) - timedelta(hours=3), "run_id": stale_run_id},
            )
    finally:
        await engine.dispose()

    recovered_run_id = await storage.begin_sync_run(
        trigger="webhook", item_id="item-investments", configured_windows={}
    )
    engine = create_async_engine(db_url)
    try:
        async with engine.connect() as conn:
            old_status = (
                await conn.execute(
                    text("SELECT status FROM sync_runs WHERE run_id = :run_id"), {"run_id": stale_run_id}
                )
            ).scalar_one()
            new_status = (
                await conn.execute(
                    text("SELECT status FROM sync_runs WHERE run_id = :run_id"), {"run_id": recovered_run_id}
                )
            ).scalar_one()
        assert old_status == "failed"
        assert new_status == "running"
    finally:
        await engine.dispose()


async def test_item_sync_queue_coalesces_events_during_a_claim(storage: PlaidLinkStorage) -> None:
    await storage.upsert_link(
        item_id="item-transactions",
        access_token_secret="item-transactions-token",
        products_requested=["transactions"],
        institution_id="ins_transactions",
        institution_name="Transactions Test",
        label=None,
    )
    await storage.enqueue_item_sync("item-transactions")
    first_claim = await storage.claim_item_sync()
    assert first_claim is not None
    await storage.enqueue_item_sync("item-transactions")
    await storage.finish_item_sync(first_claim)

    second_claim = await storage.claim_item_sync()
    assert second_claim is not None
    assert second_claim.generation == first_claim.generation + 1
    await storage.finish_item_sync(second_claim)
    assert await storage.claim_item_sync() is None


async def test_plaid_webhook_delivery_keeps_full_body_and_dispatch_metadata(
    storage: PlaidLinkStorage, db_url: str
) -> None:
    raw_body = '{"webhook_type":"ITEM","webhook_code":"WEBHOOK_UPDATE_ACKNOWLEDGED","extra":{"value":42}}'
    delivery_id = await storage.record_plaid_webhook_delivery(raw_body)
    await storage.update_plaid_webhook_delivery(
        delivery_id,
        webhook_type="ITEM",
        webhook_code="WEBHOOK_UPDATE_ACKNOWLEDGED",
        item_id=None,
        disposition="ignored",
    )

    engine = create_async_engine(db_url)
    try:
        async with engine.connect() as conn:
            delivery = (
                (
                    await conn.execute(
                        text(
                            "SELECT raw_body, webhook_type, webhook_code, item_id, disposition "
                            "FROM plaid_webhook_deliveries WHERE id = :delivery_id"
                        ),
                        {"delivery_id": delivery_id},
                    )
                )
                .mappings()
                .one()
            )
        assert delivery == {
            "raw_body": raw_body,
            "webhook_type": "ITEM",
            "webhook_code": "WEBHOOK_UPDATE_ACKNOWLEDGED",
            "item_id": None,
            "disposition": "ignored",
        }
    finally:
        await engine.dispose()


async def test_purge_link_data_removes_mirrored_rows_but_keeps_audit_history(
    storage: PlaidLinkStorage, db_url: str
) -> None:
    captured_at = datetime(2026, 5, 31, 12, 0, tzinfo=UTC)
    await _add_link(storage, item_id="item-purge")
    await _add_link(storage, item_id="item-keep")
    await storage.enqueue_item_sync("item-purge")

    await storage.apply_accounts(
        item_id="item-purge",
        accounts=[
            {
                "account_id": "account-purge",
                "name": "Purge Checking",
                "type": "depository",
                "balances": {"available": 10.0, "current": 11.0, "limit": None, "iso_currency_code": "USD"},
            }
        ],
        captured_at=captured_at,
    )
    await storage.apply_accounts(
        item_id="item-keep",
        accounts=[
            {
                "account_id": "account-keep",
                "name": "Keep Brokerage",
                "type": "investment",
                "balances": {"available": None, "current": 20.0, "limit": None, "iso_currency_code": "USD"},
            }
        ],
        captured_at=captured_at,
    )
    await storage.apply_transaction_delta(
        item_id="item-purge",
        added=[
            {
                "transaction_id": "txn-purge",
                "account_id": "account-purge",
                "date": "2026-05-30",
                "amount": 12.34,
                "name": "Coffee",
                "pending": False,
            }
        ],
        modified=[],
        removed=[],
        next_cursor="cursor-purge",
        captured_at=captured_at,
    )
    await storage.apply_holdings(
        item_id="item-purge",
        securities=[
            {"security_id": "security-purge", "name": "Purge Fund", "raw_json": {}},
            {"security_id": "security-shared", "name": "Shared Fund", "raw_json": {}},
        ],
        holdings=[
            {"account_id": "account-purge", "security_id": "security-purge", "quantity": 1.0},
            {"account_id": "account-purge", "security_id": "security-shared", "quantity": 2.0},
        ],
        captured_at=captured_at,
    )
    await storage.apply_holdings(
        item_id="item-keep",
        securities=[{"security_id": "security-shared", "name": "Shared Fund", "raw_json": {}}],
        holdings=[{"account_id": "account-keep", "security_id": "security-shared", "quantity": 3.0}],
        captured_at=captured_at,
    )
    await storage.upsert_investment_transactions(
        item_id="item-purge",
        transactions=[
            {
                "investment_transaction_id": "investment-txn-purge",
                "account_id": "account-purge",
                "security_id": "security-purge",
                "date": "2026-05-30",
            }
        ],
        captured_at=captured_at,
    )
    await storage.append_liability_snapshots(
        item_id="item-purge",
        liabilities={
            "credit": [{"account_id": "account-purge", "raw_json": {"kind": "credit"}}],
            "mortgage": [{"account_id": "account-purge", "raw_json": {"kind": "mortgage"}}],
            "student": [{"account_id": "account-purge", "raw_json": {"kind": "student"}}],
        },
        captured_at=captured_at,
    )
    run_id = await storage.begin_sync_run(trigger="manual", item_id="item-purge", configured_windows={})
    await storage.record_api_event(
        ApiEvent(
            sync_run_id=run_id,
            endpoint="transactions/get",
            item_id="item-purge",
            status="ok",
            request_json={"item_id": "item-purge"},
            response_json={"transactions": []},
        )
    )

    await storage.purge_link_data("item-purge")

    engine = create_async_engine(db_url)
    try:
        async with engine.connect() as conn:
            for table in (
                "links",
                "accounts",
                "transactions",
                "balance_snapshots",
                "holding_snapshots",
                "investment_transactions",
                "liability_credit_snapshots",
                "liability_mortgage_snapshots",
                "liability_student_snapshots",
            ):
                assert (
                    await conn.execute(text(f"SELECT count(*) FROM {table} WHERE item_id = 'item-purge'"))
                ).scalar_one() == 0
            assert (
                await conn.execute(text("SELECT count(*) FROM links WHERE item_id = 'item-keep'"))
            ).scalar_one() == 1
            assert (
                await conn.execute(text("SELECT count(*) FROM securities WHERE security_id = 'security-purge'"))
            ).scalar_one() == 0
            assert (
                await conn.execute(text("SELECT count(*) FROM securities WHERE security_id = 'security-shared'"))
            ).scalar_one() == 1
            assert (
                await conn.execute(text("SELECT count(*) FROM sync_runs WHERE item_id = 'item-purge'"))
            ).scalar_one() == 1
            assert (
                await conn.execute(text("SELECT count(*) FROM item_sync_queue WHERE item_id = 'item-purge'"))
            ).scalar_one() == 0
            assert (
                await conn.execute(text("SELECT count(*) FROM plaid_api_events WHERE item_id = 'item-purge'"))
            ).scalar_one() == 1
    finally:
        await engine.dispose()


if __name__ == "__main__":
    pytest_bazel.main()
