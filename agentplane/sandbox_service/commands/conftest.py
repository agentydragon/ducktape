"""Isolated, migrated PostgreSQL fixture for the command admission store."""

from collections.abc import AsyncIterator, Iterator
from uuid import uuid4

import pytest
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from testcontainers.postgres import PostgresContainer

from agentplane.sandbox_service.commands.database_migrate import RUNNER
from util.testing.postgres import create_database_sync, force_drop_database_sync
from util.testing.postgres_fixtures import postgres_container

# gazelle:include_dep @pypi//asyncpg
# gazelle:include_dep @pypi//psycopg


@pytest.fixture
def db_url(postgres_container: PostgresContainer) -> Iterator[str]:
    admin = f"postgresql+psycopg://postgres:postgres@{postgres_container.get_container_host_ip()}:{postgres_container.get_exposed_port(5432)}/postgres"
    name = f"commands_{uuid4().hex}"
    url = (
        make_url(create_database_sync(admin, name))
        .set(drivername="postgresql+asyncpg")
        .render_as_string(hide_password=False)
    )
    RUNNER.apply(url)
    yield url
    force_drop_database_sync(admin, name)


@pytest.fixture
async def engine(db_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(db_url)
    try:
        yield engine
    finally:
        await engine.dispose()
