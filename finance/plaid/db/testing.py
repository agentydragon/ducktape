"""Shared migrated database fixture for the Plaid PostgreSQL tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import select

from finance.plaid.db.link_store import PlaidLinkStorage
from finance.plaid.db.models import SyncRunRow
from util.db_url import render_postgresql_url
from util.testing.postgres import DatabaseClones, PostgresDatabaseTemplate

# SQLAlchemy loads asyncpg from the URL scheme; Gazelle cannot infer the dependency.
# gazelle:include_dep @pypi//asyncpg
# SQLAlchemy loads psycopg from the admin URL scheme; Gazelle cannot infer that dependency either.
# gazelle:include_dep @pypi//psycopg


async def ensure_test_sync_run(storage: PlaidLinkStorage, *, item_id: str) -> UUID:
    """Keep test mirror observations under one running Item sync."""
    async with storage._session_factory() as session:
        run_id = await session.scalar(
            select(SyncRunRow.run_id).where(SyncRunRow.item_id == item_id, SyncRunRow.status == "running")
        )
    return run_id or await storage.begin_sync_run(trigger="test", item_id=item_id, configured_windows={})


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def plaid_database_template(postgres_admin_url: str) -> AsyncIterator[PostgresDatabaseTemplate]:
    """Migrate the Plaid schema once per test process; callers clone it per test."""
    with PostgresDatabaseTemplate(postgres_admin_url, "plaid_test_template") as template:
        async_url = render_postgresql_url(template.url, "postgresql+asyncpg")
        storage = await PlaidLinkStorage.initialize(async_url)
        await storage.close()
        yield template


@pytest.fixture
def db_url(plaid_database_template: PostgresDatabaseTemplate, database_clones: DatabaseClones) -> str:
    """A fresh writable clone of the migrated Plaid schema for this test."""
    return database_clones.clone(plaid_database_template, drivername="postgresql+asyncpg")
