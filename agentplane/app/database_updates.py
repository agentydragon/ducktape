"""Replica-local invalidations of durable state, per NOTIFY channel, including reconnect gaps."""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from enum import StrEnum
from typing import Any

import asyncpg
from sqlalchemy import func, select
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncSession

from agentplane.app.changes import Changes

logger = logging.getLogger(__name__)


class Channel(StrEnum):
    """What a replica listens for. A notification carries no payload -- no thread and no identity --
    so its readers re-read what they follow."""

    # A thread, its copied event log or its feed state was written.
    THREADS = "agentplane_thread_updates"
    # A browser session row was deleted: logout, the rotation a login makes, or expiry cleanup.
    OPERATOR_SESSIONS = "agentplane_operator_sessions"


class DatabaseUpdates:
    """One LISTEN connection per replica, fanning each channel out to that channel's `Changes`."""

    def __init__(self, database_url: URL) -> None:
        self._dsn = database_url.set(drivername="postgresql").render_as_string(hide_password=False)
        # Per channel, every commit in the database that announced itself on it, as this replica hears of it.
        self.changes = {channel: Changes() for channel in Channel}
        self._connection: asyncpg.Connection[Any] | None = None
        self._lost = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    @property
    def connected(self) -> bool:
        # `_lost` and not the connection alone: `_terminated` wakes consumers and only then does
        # `_recover` clear `_connection`, so between those a reader woken *by* the loss would
        # otherwise be told the listener is live whenever asyncpg has yet to flip `is_closed()`.
        return not self._lost.is_set() and self._connection is not None and not self._connection.is_closed()

    async def wait_until_disconnected(self) -> None:
        """Wait for the termination callback to mark the listener unavailable."""
        await self._lost.wait()

    async def start(self) -> None:
        if self._task is not None:
            raise RuntimeError("database update listener already started")
        await self._connect()
        self._task = asyncio.create_task(self._recover())

    async def _connect(self) -> None:
        self._lost.clear()
        connection = await asyncpg.connect(
            self._dsn, timeout=10, server_settings={"application_name": "agentplane-database-updates"}
        )
        try:
            connection.add_termination_listener(self._terminated)
            for channel in Channel:
                await connection.add_listener(channel, self._notified)
            if connection.is_closed():
                raise ConnectionError("database update listener disconnected during startup")
        except BaseException:
            await connection.close(timeout=2)
            raise
        self._connection = connection
        # LISTEN is not a durable queue. Every successful reconnect requires a database read on every
        # channel, even if no notification arrives after it (all writes may have happened in the gap).
        self._wake_all()

    async def _recover(self) -> None:
        while True:
            await self._lost.wait()
            await self._disconnect()
            await asyncio.sleep(1)
            try:
                await self._connect()
            except Exception:
                self._lost.set()
                logger.exception("database update listener reconnect failed")

    async def _disconnect(self) -> None:
        if self._connection is not None:
            connection, self._connection = self._connection, None
            await connection.close(timeout=2)

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        await self._disconnect()

    def _notified(self, _connection: object, _pid: int, channel: str, _payload: object) -> None:
        self.changes[Channel(channel)].notify()

    def _terminated(self, _connection: object) -> None:
        self._lost.set()
        self._wake_all()

    def _wake_all(self) -> None:
        for changes in self.changes.values():
            changes.notify()


async def notify(session: AsyncSession, channel: Channel) -> None:
    # PostgreSQL delivers NOTIFY only on commit, to every replica listening.
    await session.execute(select(func.pg_notify(channel, "")))
