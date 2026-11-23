"""Postgres database fixture wiring for the Spend Service tests."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from finance.plaid.db.testing import db_url, plaid_database_template
from testing.postgres_fixtures import (
    database_clones,
    database_names,
    postgres_admin_url,
    postgres_container,
    postgres_image,
)
from util.db_url import render_postgresql_url

# SQLAlchemy loads both drivers from the URL schemes at runtime.
# gazelle:include_dep @pypi//asyncpg
# gazelle:include_dep @pypi//psycopg


@pytest.fixture
def postgres_url(db_url: str) -> str:
    """Return the cloned Plaid database URL in a driver-neutral form."""
    return render_postgresql_url(db_url, "postgresql")


@pytest.fixture
def postgres_engine(postgres_url: str) -> Iterator[Engine]:
    engine = create_engine(render_postgresql_url(postgres_url, "postgresql+psycopg"))
    try:
        yield engine
    finally:
        engine.dispose()


@pytest_asyncio.fixture
async def postgres_session_factory(postgres_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(render_postgresql_url(postgres_url, "postgresql+asyncpg"))
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()
