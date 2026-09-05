"""Real-Postgres fixtures for the standalone Action Service."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Iterator

import pytest
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine
from testcontainers.postgres import PostgresContainer

from util.testing.postgres import create_database_sync, force_drop_database_sync
from util.testing.postgres_fixtures import postgres_container
from x.agentplane.action_service.database_migrate import apply_migrations
from x.agentplane.action_service.db import make_engine

# SQLAlchemy loads these dialects from URLs; Gazelle cannot infer them.
# gazelle:include_dep @pypi//asyncpg
# gazelle:include_dep @pypi//psycopg


@pytest.fixture
def db_url(postgres_container: PostgresContainer, request: pytest.FixtureRequest) -> Iterator[str]:
    admin_url = (
        f"postgresql+psycopg://postgres:postgres@{postgres_container.get_container_host_ip()}"
        f":{postgres_container.get_exposed_port(5432)}/postgres"
    )
    db_name = re.sub(r"[^a-z0-9_]", "_", request.node.name.lower())[:45].rstrip("_")
    url = create_database_sync(admin_url, db_name)
    async_url = make_url(url).set(drivername="postgresql+asyncpg").render_as_string(hide_password=False)
    apply_migrations(async_url)
    yield async_url
    force_drop_database_sync(admin_url, db_name)


@pytest.fixture
async def engine(db_url: str) -> AsyncIterator[AsyncEngine]:
    engine = make_engine(db_url)
    try:
        yield engine
    finally:
        await engine.dispose()
