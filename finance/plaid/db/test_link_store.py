from __future__ import annotations

import re
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, date, datetime

import pytest
import pytest_bazel
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from testcontainers.postgres import PostgresContainer

from finance.plaid.db.link_profiles import LinkProfile
from finance.plaid.db.link_store import ApiEvent, PlaidLinkStorage
from third_party.containers.rlocations import POSTGRES_18, RYUK
from util.oci import load_oci_image
from util.testing.postgres import force_drop_database


@pytest.fixture(scope="session", autouse=True)
def _preload_postgres_images() -> None:
    load_oci_image(RYUK)
    load_oci_image(POSTGRES_18)


@pytest.fixture(scope="session")
def postgres_container() -> Generator[PostgresContainer]:
    container = PostgresContainer(image=POSTGRES_18.tag, username="postgres", password="postgres", dbname="postgres")
    container.start()
    try:
        yield container
    finally:
        container.stop()


@pytest.fixture(scope="session")
def postgres_admin_url(postgres_container: PostgresContainer) -> str:
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
        link_profile=LinkProfile.INVESTMENTS_FULL,
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


async def test_transaction_reconciliation_does_not_mark_partial_sync_success(storage: PlaidLinkStorage) -> None:
    await storage.upsert_link(
        item_id="item-transactions",
        access_token_secret="item-transactions-token",
        link_profile=LinkProfile.CASHFLOW,
        products_requested=["transactions"],
        institution_id="ins_transactions",
        institution_name="Transactions Test",
        label=None,
    )

    await storage.reconcile_transactions(
        item_id="item-transactions",
        start_date=date(2026, 5, 1),
        end_date=date(2026, 5, 31),
        transactions=[],
        captured_at=datetime(2026, 5, 31, 12, 0, tzinfo=UTC),
    )

    link = await storage.get_link("item-transactions")
    assert link is not None
    assert link.last_synced_at is None


async def test_purge_link_data_removes_mirrored_rows_but_keeps_audit_history(
    storage: PlaidLinkStorage, db_url: str
) -> None:
    captured_at = datetime(2026, 5, 31, 12, 0, tzinfo=UTC)
    await _add_link(storage, item_id="item-purge")
    await _add_link(storage, item_id="item-keep")

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
    await storage.reconcile_transactions(
        item_id="item-purge",
        start_date=date(2026, 5, 1),
        end_date=date(2026, 5, 31),
        transactions=[
            {
                "transaction_id": "txn-purge",
                "account_id": "account-purge",
                "date": "2026-05-30",
                "amount": 12.34,
                "name": "Coffee",
                "pending": False,
            }
        ],
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
                await conn.execute(text("SELECT count(*) FROM plaid_api_events WHERE item_id = 'item-purge'"))
            ).scalar_one() == 1
    finally:
        await engine.dispose()


if __name__ == "__main__":
    pytest_bazel.main()
