"""Isolated, migrated PostgreSQL fixture for the command admission store."""

from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from agentplane.runner_gateway.commands.database_migrate import RUNNER
from util.testing.postgres_fixtures import (
    database_clones,
    database_names,
    migrated_database_fixtures,
    postgres_admin_url,
    postgres_container,
)

# gazelle:include_dep @pypi//asyncpg
# gazelle:include_dep @pypi//psycopg


database_template, db_url = migrated_database_fixtures(RUNNER, drivername="postgresql+asyncpg")


@pytest.fixture
async def engine(db_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(db_url)
    try:
        yield engine
    finally:
        await engine.dispose()
