"""Isolated PostgreSQL/pgvector databases for index service tests."""

from collections.abc import AsyncGenerator, Generator

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from testcontainers.postgres import PostgresContainer

from haku.recall_index.chunking import DEFAULT_CHUNK_BUDGET
from haku.recall_index.fake_embedder import FakeEmbedder
from third_party.containers.rlocations import PGVECTOR_PG18
from util.testing.postgres_fixtures import start_postgres_container
from x.agentplane.indexing.store import Store

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
    host = pgvector_container.get_container_host_ip()
    port = int(pgvector_container.get_exposed_port(5432))
    opened = create_async_engine(f"postgresql+asyncpg://postgres:postgres@{host}:{port}/postgres")
    try:
        async with opened.begin() as connection:
            await connection.exec_driver_sql("DROP SCHEMA IF EXISTS agentplane_index CASCADE")
        yield opened
    finally:
        await opened.dispose()


@pytest.fixture
def embedder() -> FakeEmbedder:
    return FakeEmbedder()


@pytest.fixture
async def store(engine: AsyncEngine, embedder: FakeEmbedder) -> Store:
    result = Store(engine, budget=DEFAULT_CHUNK_BUDGET, model_key=embedder.model_key)
    await result.initialize()
    return result
