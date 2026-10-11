"""Postgres-with-pgvector fixtures for the index's database tests."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Generator

import pytest
import pytest_asyncio
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from testcontainers.postgres import PostgresContainer

from haku.recall_index.fake_embedder import FakeEmbedder
from haku.recall_index.store import ensure_schema
from third_party.containers import pgvector_pg18
from util.testing.postgres import DatabaseClones, PostgresDatabaseTemplate
from util.testing.postgres_fixtures import database_clones, database_names, postgres_admin_url, start_postgres_container

# Fixtures build postgresql+asyncpg:// engines; SQLAlchemy loads the dialect's
# driver at runtime, so gazelle cannot see the dependency.
# gazelle:include_dep @pypi//asyncpg
# The template admin URL uses postgresql+psycopg://, also loaded dynamically by SQLAlchemy.
# gazelle:include_dep @pypi//psycopg


@pytest.fixture(scope="session")
def postgres_container() -> Generator[PostgresContainer]:
    container = start_postgres_container(pgvector_pg18.IMAGE)
    try:
        yield container
    finally:
        container.stop()


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def recall_index_database_template(postgres_admin_url: str) -> AsyncGenerator[PostgresDatabaseTemplate]:
    """Create the pgvector schema once; each test gets a writable database clone."""
    with PostgresDatabaseTemplate(postgres_admin_url, "recall_index_template", extensions=("vector",)) as template:
        engine = create_async_engine(make_url(template.url).set(drivername="postgresql+asyncpg"))
        try:
            await ensure_schema(engine)
        finally:
            await engine.dispose()
        yield template


@pytest.fixture
async def engine(
    recall_index_database_template: PostgresDatabaseTemplate, database_clones: DatabaseClones
) -> AsyncGenerator[AsyncEngine]:
    """An engine on a fresh, writable clone of the initialized schema."""
    url = database_clones.clone(recall_index_database_template, drivername="postgresql+asyncpg")
    opened = create_async_engine(make_url(url))
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
