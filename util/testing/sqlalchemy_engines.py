"""SQLAlchemy engine lifetime helpers for tests and their service contexts."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncEngine


@asynccontextmanager
async def managed_async_engine(engine: AsyncEngine) -> AsyncIterator[AsyncEngine]:
    """Own an engine until context exit, preserving the caller's construction settings."""
    try:
        yield engine
    finally:
        await engine.dispose()
