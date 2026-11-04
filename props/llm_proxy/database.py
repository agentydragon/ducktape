"""Async PostgreSQL sessions owned by the standalone LLM proxy."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from props.db.config import DatabaseConfig
from util.db_url import async_engine_url


class ProxyDatabase:
    def __init__(self, config: DatabaseConfig) -> None:
        self.config = config
        self.engine = create_async_engine(async_engine_url(config.url), poolclass=NullPool)
        self._sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    async def verify(self) -> None:
        async with asyncio.timeout(2):
            async with self.engine.connect() as connection:
                await connection.execute(text("SELECT 1"))

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self._sessions() as session:
            yield session
            await session.commit()

    async def dispose(self) -> None:
        await self.engine.dispose()

    async def __aenter__(self) -> ProxyDatabase:
        return self

    async def __aexit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        await self.dispose()


def get_proxy_db(request: Request) -> ProxyDatabase:
    return request.app.state.admin_db  # type: ignore[no-any-return]


ProxyDb = Annotated[ProxyDatabase, Depends(get_proxy_db)]
