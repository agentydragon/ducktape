"""Layer-neutral probes for the wake tests, against a real Postgres.

Shared by `test_session_wakes.py` and `test_conversation_wakes.py`: the delivery/absence assertions
and the raw-emit and listener-kill helpers are the same whichever channel is under test.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable

import asyncpg
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from haku.console.x.pg_wake import libpq_dsn, notify_raw


@pytest.fixture
async def raw_payloads(migrated_db_url: str) -> AsyncIterator[Callable[[str], Awaitable[asyncio.Queue[str]]]]:
    """A raw listener per channel, so a wire pin has no code under test on its reading end."""
    connections: list[asyncpg.Connection[asyncpg.Record]] = []

    async def listen(channel: str) -> asyncio.Queue[str]:
        received: asyncio.Queue[str] = asyncio.Queue()
        connection = await asyncpg.connect(libpq_dsn(migrated_db_url))
        connections.append(connection)
        await connection.add_listener(channel, lambda _conn, _pid, _channel, payload: received.put_nowait(str(payload)))
        return received

    yield listen
    for connection in connections:
        await connection.close(timeout=5)


async def delivered[T](received: asyncio.Queue[T]) -> T:
    async with asyncio.timeout(30):
        return await received.get()


async def nothing_within[T](received: asyncio.Queue[T], seconds: float) -> bool:
    """Delivery is asynchronous, so an absence can only be asserted by waiting one out."""
    with contextlib.suppress(TimeoutError):
        async with asyncio.timeout(seconds):
            await received.get()
            return False
    return True


async def emit_raw(db_sessions: async_sessionmaker[AsyncSession], channel: str, payload: str) -> None:
    """`notify_raw`, with the transaction the production callers already hold opened here."""
    async with db_sessions.begin() as db:
        await notify_raw(db, channel, payload)


async def kill_listener_backends(db_sessions: async_sessionmaker[AsyncSession]) -> None:
    """The closest thing to the real failure: a database restart, a failover, a connection reaper."""
    async with db_sessions.begin() as db:
        await db.execute(
            text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = current_database() AND pid <> pg_backend_pid() "
                "AND query LIKE 'LISTEN%'"
            )
        )
