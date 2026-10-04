"""Postgres-backed link storage shared by the plaid db tests."""

from __future__ import annotations

import re
from collections.abc import AsyncGenerator, Awaitable, Callable

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from testcontainers.postgres import PostgresContainer

from finance.plaid.db.link_store import PlaidLinkStorage
from util.testing.postgres import force_drop_database
from util.testing.postgres_fixtures import postgres_container


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


@pytest.fixture
def add_link(storage: PlaidLinkStorage) -> Callable[[str], Awaitable[None]]:
    async def add(item_id: str) -> None:
        await storage.upsert_link(
            item_id=item_id,
            access_token_secret=f"{item_id}-token",
            products_requested=["investments"],
            institution_id="ins_investments",
            institution_name="Investment Test",
            label=None,
        )

    return add
