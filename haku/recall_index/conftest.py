"""Postgres-with-pgvector fixtures for the index's database tests."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Generator
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from testcontainers.postgres import PostgresContainer

from haku.recall_index.fake_embedder import FakeEmbedder
from haku.recall_index.store import ensure_schema
from third_party.containers import pgvector_pg18
from util.testing.postgres import PostgresDatabaseTemplate, database_name_for_test
from util.testing.postgres_fixtures import start_postgres_container

# Fixtures build postgresql+asyncpg:// engines; SQLAlchemy loads the dialect's
# driver at runtime, so gazelle cannot see the dependency.
# gazelle:include_dep @pypi//asyncpg
# The template admin URL uses postgresql+psycopg://, also loaded dynamically by SQLAlchemy.
# gazelle:include_dep @pypi//psycopg


@pytest.fixture(scope="session")
def pgvector_container() -> Generator[PostgresContainer]:
    container = start_postgres_container(pgvector_pg18.IMAGE)
    try:
        yield container
    finally:
        container.stop()


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def recall_index_database_template(
    pgvector_container: PostgresContainer,
) -> AsyncGenerator[PostgresDatabaseTemplate]:
    """Create the pgvector schema once; each test gets a writable database clone."""
    host = pgvector_container.get_container_host_ip()
    port = int(pgvector_container.get_exposed_port(5432))
    admin_url = f"postgresql+psycopg://postgres:postgres@{host}:{port}/postgres"
    with PostgresDatabaseTemplate(
        admin_url, f"recall_index_template_{uuid4().hex}", extensions=("vector",)
    ) as template:
        engine = create_async_engine(make_url(template.url).set(drivername="postgresql+asyncpg"))
        try:
            await ensure_schema(engine)
        finally:
            await engine.dispose()
        yield template


@pytest.fixture
async def engine(
    recall_index_database_template: PostgresDatabaseTemplate, request: pytest.FixtureRequest
) -> AsyncGenerator[AsyncEngine]:
    """An engine on a fresh, writable clone of the initialized schema."""
    with recall_index_database_template.clone(database_name_for_test(request.node.nodeid)) as url:
        opened = create_async_engine(make_url(url).set(drivername="postgresql+asyncpg"))
        try:
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
