from __future__ import annotations

import re
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime

import pytest
import pytest_asyncio
import pytest_bazel
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from testcontainers.postgres import PostgresContainer

from finance.plaid.db.link_profiles import LinkProfile
from finance.plaid.db.link_store import PlaidLinkStorage
from finance.plaid.db.read_model import read_current_cash_balances, read_current_holdings
from finance.plaid.db.schema import async_session_factory
from util.testing.postgres import force_drop_database
from util.testing.postgres_fixtures import start_postgres_container


@pytest.fixture(scope="session")
def postgres_container() -> Generator[PostgresContainer]:
    container = start_postgres_container()
    try:
        yield container
    finally:
        container.stop()


@pytest.fixture(scope="session")
def postgres_admin_url(postgres_container: PostgresContainer) -> str:
    host = postgres_container.get_container_host_ip()
    port = int(postgres_container.get_exposed_port(5432))
    return f"postgresql+asyncpg://postgres:postgres@{host}:{port}/postgres"


@pytest_asyncio.fixture
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


@pytest_asyncio.fixture
async def storage(db_url: str) -> AsyncGenerator[PlaidLinkStorage]:
    store = await PlaidLinkStorage.initialize(db_url)
    try:
        yield store
    finally:
        await store.close()


@pytest_asyncio.fixture
async def session_factory(db_url: str) -> AsyncGenerator[async_sessionmaker[AsyncSession]]:
    engine, factory = async_session_factory(db_url)
    try:
        yield factory
    finally:
        await engine.dispose()


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


async def test_read_current_cash_balances_returns_latest_selected_usd_snapshot(
    storage: PlaidLinkStorage, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await _add_link(storage, item_id="item-cash")
    await storage.apply_accounts(
        item_id="item-cash",
        accounts=[
            {
                "account_id": "checking",
                "name": "Checking",
                "type": "depository",
                "balances": {"available": 100.0, "current": 110.0, "limit": None, "iso_currency_code": "USD"},
            },
            {
                "account_id": "cad",
                "name": "CAD",
                "type": "depository",
                "balances": {"available": 5.0, "current": 6.0, "limit": None, "iso_currency_code": "CAD"},
            },
        ],
        captured_at=datetime(2026, 5, 31, 12, 0, tzinfo=UTC),
    )
    await storage.apply_accounts(
        item_id="item-cash",
        accounts=[
            {
                "account_id": "checking",
                "name": "Checking",
                "type": "depository",
                "balances": {"available": 150.0, "current": 160.0, "limit": None, "iso_currency_code": "USD"},
            }
        ],
        captured_at=datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
    )

    balances = await read_current_cash_balances(
        session_factory=session_factory, account_ids=("checking", "cad", "missing")
    )

    assert [balance.account_id for balance in balances] == ["checking"]
    assert balances[0].current == 160.0
    assert balances[0].available == 150.0
    assert balances[0].institution_name == "Investment Test"


async def test_read_current_holdings_returns_latest_selected_usd_holdings(
    storage: PlaidLinkStorage, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await _add_link(storage, item_id="item-investments")
    await storage.apply_accounts(
        item_id="item-investments",
        accounts=[
            {
                "account_id": "brokerage",
                "name": "Brokerage",
                "type": "investment",
                "balances": {"available": None, "current": 1000.0, "limit": None, "iso_currency_code": "USD"},
            }
        ],
        captured_at=datetime(2026, 5, 31, 12, 0, tzinfo=UTC),
    )
    securities = [
        {"security_id": "sec-voo", "name": "Vanguard 500", "ticker_symbol": "VOO", "type": "etf", "raw_json": {}},
        {"security_id": "sec-cad", "name": "CAD Fund", "ticker_symbol": "CADF", "type": "etf", "raw_json": {}},
    ]
    await storage.apply_holdings(
        item_id="item-investments",
        securities=securities,
        holdings=[
            {
                "account_id": "brokerage",
                "security_id": "sec-voo",
                "quantity": 2.0,
                "cost_basis": 700.0,
                "institution_price": 400.0,
                "institution_value": 800.0,
                "iso_currency_code": "USD",
            },
            {
                "account_id": "brokerage",
                "security_id": "sec-cad",
                "quantity": 1.0,
                "cost_basis": 10.0,
                "institution_price": 11.0,
                "institution_value": 11.0,
                "iso_currency_code": "CAD",
            },
        ],
        captured_at=datetime(2026, 5, 31, 12, 0, tzinfo=UTC),
    )
    await storage.apply_holdings(
        item_id="item-investments",
        securities=securities,
        holdings=[
            {
                "account_id": "brokerage",
                "security_id": "sec-voo",
                "quantity": 3.0,
                "cost_basis": 900.0,
                "institution_price": 450.0,
                "institution_value": 1350.0,
                "iso_currency_code": "USD",
            }
        ],
        captured_at=datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
    )

    holdings = await read_current_holdings(session_factory=session_factory, account_ids=("brokerage", "missing"))

    assert [holding.security_id for holding in holdings] == ["sec-voo"]
    assert holdings[0].ticker_symbol == "VOO"
    assert holdings[0].quantity == 3.0
    assert holdings[0].institution_value == 1350.0
    assert holdings[0].cost_basis == 900.0


if __name__ == "__main__":
    pytest_bazel.main()
