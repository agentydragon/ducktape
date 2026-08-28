"""Postgres-with-pgvector fixtures for the index's database tests."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Generator

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from testcontainers.postgres import PostgresContainer

from haku.recall_index.fake_embedder import FakeEmbedder
from haku.recall_index.store import ensure_schema
from third_party.containers.rlocations import PGVECTOR_PG18
from util.testing.postgres_fixtures import start_postgres_container

# Fixtures build postgresql+asyncpg:// engines; SQLAlchemy loads the dialect's
# driver at runtime, so gazelle cannot see the dependency.
# gazelle:include_dep @pypi//asyncpg


@pytest.fixture(scope="session")
def pgvector_container() -> Generator[PostgresContainer]:
    container = start_postgres_container(PGVECTOR_PG18)
    try:
        yield container
    finally:
        container.stop()


@pytest.fixture
async def engine(pgvector_container: PostgresContainer) -> AsyncGenerator[AsyncEngine]:
    """An engine on a schema created fresh for each test."""
    host = pgvector_container.get_container_host_ip()
    port = int(pgvector_container.get_exposed_port(5432))
    opened = create_async_engine(f"postgresql+asyncpg://postgres:postgres@{host}:{port}/postgres")
    try:
        async with opened.begin() as connection:
            await connection.exec_driver_sql("DROP SCHEMA IF EXISTS recall_index CASCADE")
            # `public` holds the console tables the chat corpus reads, and the `vector`
            # extension. Both are reset here, before `ensure_schema` puts the extension back —
            # dropping it afterwards would take the vector columns with it.
            await connection.exec_driver_sql("DROP SCHEMA IF EXISTS public CASCADE")
            await connection.exec_driver_sql("CREATE SCHEMA public")
        await ensure_schema(opened)
        yield opened
    finally:
        await opened.dispose()


@pytest.fixture
async def session(engine: AsyncEngine) -> AsyncGenerator[AsyncSession]:
    """A session on the per-test schema, for the tests that need only one."""
    async with async_sessionmaker(engine, expire_on_commit=False)() as opened:
        yield opened


@pytest.fixture
def embedder() -> FakeEmbedder:
    """The default fake embedder, for the tests that only need *an* embedder.

    A test asserting what a regime change does builds its own with a different `model_key`, and
    `ExplodingEmbedder` is its own class — those are the subject of their tests, not setup.
    """
    return FakeEmbedder()
