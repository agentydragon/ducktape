"""Shared migrated database fixture for the Plaid PostgreSQL tests."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy.engine import make_url
from testcontainers.postgres import PostgresContainer

from finance.plaid.db.link_store import PlaidLinkStorage
from util.testing.postgres import PostgresDatabaseTemplate, database_name_for_test

# SQLAlchemy loads asyncpg from the URL scheme; Gazelle cannot infer the dependency.
# gazelle:include_dep @pypi//asyncpg
# SQLAlchemy loads psycopg from the admin URL scheme; Gazelle cannot infer that dependency either.
# gazelle:include_dep @pypi//psycopg


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def plaid_database_template(postgres_container: PostgresContainer) -> AsyncIterator[PostgresDatabaseTemplate]:
    """Migrate the Plaid schema once per test process; callers clone it per test."""
    host = postgres_container.get_container_host_ip()
    port = postgres_container.get_exposed_port(5432)
    admin_url = f"postgresql+psycopg://postgres:postgres@{host}:{port}/postgres"
    with PostgresDatabaseTemplate(admin_url, f"plaid_test_template_{uuid4().hex}") as template:
        async_url = make_url(template.url).set(drivername="postgresql+asyncpg").render_as_string(hide_password=False)
        storage = await PlaidLinkStorage.initialize(async_url)
        await storage.close()
        yield template


@pytest.fixture
def plaid_database_url(
    plaid_database_template: PostgresDatabaseTemplate, request: pytest.FixtureRequest
) -> Iterator[str]:
    """A fresh writable clone of the migrated Plaid schema for this test."""
    db_name = database_name_for_test(request.node.nodeid)
    with plaid_database_template.clone(db_name) as url:
        yield make_url(url).set(drivername="postgresql+asyncpg").render_as_string(hide_password=False)
