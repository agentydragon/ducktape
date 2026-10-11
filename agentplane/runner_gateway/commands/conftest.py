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
from util.testing.sqlalchemy_engines import managed_async_engine

# gazelle:include_dep @pypi//asyncpg
# gazelle:include_dep @pypi//psycopg


database_template, db_url = migrated_database_fixtures(RUNNER, drivername="postgresql+asyncpg")


@pytest.fixture
async def engine(db_url: str) -> AsyncIterator[AsyncEngine]:
    async with managed_async_engine(create_async_engine(db_url)) as engine:
        yield engine
