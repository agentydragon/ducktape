"""Shared migrated database fixture for the Plaid PostgreSQL tests."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

from finance.plaid.db.link_store import PlaidLinkStorage
from util.db_url import render_postgresql_url
from util.testing.postgres import DatabaseClones, PostgresDatabaseTemplate

# SQLAlchemy loads asyncpg from the URL scheme; Gazelle cannot infer the dependency.
# gazelle:include_dep @pypi//asyncpg
# SQLAlchemy loads psycopg from the admin URL scheme; Gazelle cannot infer that dependency either.
# gazelle:include_dep @pypi//psycopg


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
