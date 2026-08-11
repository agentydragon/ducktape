"""Postgres-with-pgvector fixtures for the index's database tests."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Generator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from testcontainers.postgres import PostgresContainer

from haku.state_index.store import ensure_schema
from third_party.containers.rlocations import PGVECTOR_PG18
from util.testing.postgres_fixtures import start_postgres_container


@pytest.fixture(scope="session")
def pgvector_container() -> Generator[PostgresContainer]:
    container = start_postgres_container(PGVECTOR_PG18)
    try:
        yield container
    finally:
        container.stop()


@pytest.fixture
async def session(pgvector_container: PostgresContainer) -> AsyncGenerator[AsyncSession]:
    """A session on a schema created fresh for each test."""
    host = pgvector_container.get_container_host_ip()
    port = int(pgvector_container.get_exposed_port(5432))
    engine = create_async_engine(f"postgresql+asyncpg://postgres:postgres@{host}:{port}/postgres")
    try:
        async with engine.begin() as connection:
            await connection.exec_driver_sql("DROP SCHEMA IF EXISTS state_index CASCADE")
        await ensure_schema(engine)
        async with async_sessionmaker(engine, expire_on_commit=False)() as opened:
            yield opened
    finally:
        await engine.dispose()
