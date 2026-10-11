"""Isolated, migrated PostgreSQL fixture for the history store."""

from collections.abc import AsyncIterator, Iterator

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from agentplane.history_service.storage.database_migrate import RUNNER
from util.testing.postgres import DatabaseClones, PostgresDatabaseTemplate, migrated_database_template
from util.testing.postgres_fixtures import (
    database_clones,
    database_names,
    postgres_admin_url,
    postgres_container,
    postgres_image,
)
from util.testing.sqlalchemy_engines import managed_async_engine

# gazelle:include_dep @pypi//asyncpg
# gazelle:include_dep @pypi//psycopg


@pytest.fixture(scope="session")
def history_database_template(postgres_admin_url: str) -> Iterator[PostgresDatabaseTemplate]:
    """One migrated History Service database per test process, cloned for each case."""
    # The cluster-wide Electric login must exist before migration grants are copied to clones.
    admin_engine = create_engine(postgres_admin_url, isolation_level="AUTOCOMMIT")
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
    with migrated_database_template(postgres_admin_url, "history_template", RUNNER) as template:
        yield template


@pytest.fixture
def db_url(history_database_template: PostgresDatabaseTemplate, database_clones: DatabaseClones) -> str:
    return database_clones.clone(history_database_template, drivername="postgresql+asyncpg")


@pytest.fixture
async def engine(db_url: str) -> AsyncIterator[AsyncEngine]:
    async with managed_async_engine(create_async_engine(db_url)) as engine:
        yield engine
