"""SQLAlchemy engine lifetime helpers for tests and their service contexts."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

# SQLAlchemy loads the dialect by name.
# gazelle:include_dep @pypi//asyncpg


@asynccontextmanager
async def managed_async_engine(engine: AsyncEngine) -> AsyncIterator[AsyncEngine]:
    """Own an engine until context exit, preserving the caller's construction settings."""
    try:
        yield engine
    finally:
        await engine.dispose()


@asynccontextmanager
async def postgres_async_engine(database_url: str) -> AsyncIterator[AsyncEngine]:
    """Open a test PostgreSQL URL with asyncpg and dispose the engine on context exit."""
    async with managed_async_engine(
        create_async_engine(make_url(database_url).set(drivername="postgresql+asyncpg"))
    ) as engine:
        yield engine
