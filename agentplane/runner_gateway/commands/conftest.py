"""Isolated, migrated PostgreSQL fixture for the command admission store."""

from collections.abc import AsyncIterator, Iterator

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from agentplane.runner_gateway.commands.database_migrate import RUNNER
from util.testing.postgres import DatabaseClones, PostgresDatabaseTemplate, migrated_database_template
from util.testing.postgres_fixtures import database_clones, database_names, postgres_admin_url, postgres_container

# gazelle:include_dep @pypi//asyncpg
# gazelle:include_dep @pypi//psycopg


@pytest.fixture(scope="session")
def commands_database_template(postgres_admin_url: str) -> Iterator[PostgresDatabaseTemplate]:
    with migrated_database_template(postgres_admin_url, "commands_template", RUNNER) as template:
        yield template


@pytest.fixture
def db_url(commands_database_template: PostgresDatabaseTemplate, database_clones: DatabaseClones) -> str:
    return database_clones.clone(commands_database_template, drivername="postgresql+asyncpg")


@pytest.fixture
async def engine(db_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(db_url)
    try:
        yield engine
    finally:
        await engine.dispose()
