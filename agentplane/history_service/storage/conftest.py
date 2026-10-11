"""Isolated, migrated PostgreSQL fixture for the history store."""

from collections.abc import AsyncIterator, Iterator

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from agentplane.history_service.storage.database_migrate import RUNNER
from agentplane.testing.electric_role import ensure_electric_role
from util.testing.postgres import DatabaseClones, PostgresDatabaseTemplate, migrated_database_template
from util.testing.postgres_fixtures import database_clones, database_names, postgres_admin_url, postgres_container
from util.testing.sqlalchemy_engines import managed_async_engine

# gazelle:include_dep @pypi//asyncpg
# gazelle:include_dep @pypi//psycopg


@pytest.fixture(scope="session")
def history_database_template(postgres_admin_url: str) -> Iterator[PostgresDatabaseTemplate]:
    """One migrated History Service database per test process, cloned for each case."""
    # The cluster-wide Electric login must exist before migration grants are copied to clones.
    ensure_electric_role(postgres_admin_url)
    with migrated_database_template(postgres_admin_url, "history_template", RUNNER) as template:
        yield template


@pytest.fixture
def db_url(history_database_template: PostgresDatabaseTemplate, database_clones: DatabaseClones) -> str:
    return database_clones.clone(history_database_template, drivername="postgresql+asyncpg")


@pytest.fixture
async def engine(db_url: str) -> AsyncIterator[AsyncEngine]:
    async with managed_async_engine(create_async_engine(db_url)) as engine:
        yield engine
