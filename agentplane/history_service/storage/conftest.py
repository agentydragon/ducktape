"""Isolated, migrated PostgreSQL fixture for the history store."""

from collections.abc import AsyncIterator, Iterator
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from testcontainers.postgres import PostgresContainer

from agentplane.history_service.storage.database_migrate import RUNNER
from util.testing.postgres import PostgresDatabaseTemplate
from util.testing.postgres_fixtures import postgres_container

# gazelle:include_dep @pypi//asyncpg
# gazelle:include_dep @pypi//psycopg


@pytest.fixture(scope="session")
def history_database_template(postgres_container: PostgresContainer) -> Iterator[PostgresDatabaseTemplate]:
    """One migrated History Service database per test process, cloned for each case."""
    admin = f"postgresql+psycopg://postgres:postgres@{postgres_container.get_container_host_ip()}:{postgres_container.get_exposed_port(5432)}/postgres"
    # The cluster-wide Electric login must exist before migration grants are copied to clones.
    admin_engine = create_engine(admin, isolation_level="AUTOCOMMIT")
    try:
        with admin_engine.connect() as connection:
            connection.execute(
                text(
                    "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'electric') "
                    "THEN CREATE ROLE electric LOGIN REPLICATION PASSWORD 'electric'; END IF; END $$"
                )
            )
    finally:
        admin_engine.dispose()
    with PostgresDatabaseTemplate(admin, f"history_template_{uuid4().hex}") as template:
        RUNNER.apply(template.url)
        yield template


@pytest.fixture
def db_url(history_database_template: PostgresDatabaseTemplate) -> Iterator[str]:
    """A fresh writable clone, including the migration's Electric read grants."""
    with history_database_template.clone(f"history_{uuid4().hex}") as url:
        yield make_url(url).set(drivername="postgresql+asyncpg").render_as_string(hide_password=False)


@pytest.fixture
async def engine(db_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(db_url)
    try:
        yield engine
    finally:
        await engine.dispose()
