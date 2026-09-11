"""Replica-local invalidations of durable trajectory state, including reconnect gaps."""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from typing import Any

import asyncpg
from sqlalchemy.engine import URL

from x.agentplane.app.changes import Changes

CHANNEL = "agentplane_trajectory_updates"
logger = logging.getLogger(__name__)


class TrajectoryUpdates:
    def __init__(self, database_url: URL, changes: Changes) -> None:
        self._dsn = database_url.set(drivername="postgresql").render_as_string(hide_password=False)
        self._changes = changes
        self._connection: asyncpg.Connection[Any] | None = None
        self._lost = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is not None:
            raise RuntimeError("trajectory update listener already started")
        await self._connect()
        self._task = asyncio.create_task(self._recover())

    async def _connect(self) -> None:
        self._lost.clear()
        connection = await asyncpg.connect(
            self._dsn, timeout=10, server_settings={"application_name": "agentplane-trajectory-updates"}
        )
        try:
            connection.add_termination_listener(self._terminated)
            await connection.add_listener(CHANNEL, self._notified)
            if connection.is_closed():
                raise ConnectionError("trajectory listener disconnected during startup")
        except BaseException:
            await connection.close(timeout=2)
            raise
        self._connection = connection
        # LISTEN is not a durable queue. Every successful reconnect requires a database read,
        # even if no notification arrives after it (all writes may have happened in the gap).
        self._changes.notify()

    async def _recover(self) -> None:
        while True:
            await self._lost.wait()
            await self._disconnect()
            await asyncio.sleep(1)
            try:
                await self._connect()
            except Exception:
                self._lost.set()
                logger.exception("trajectory listener reconnect failed")

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

    def _notified(self, _connection: object, _pid: int, _channel: str, _payload: object) -> None:
        self._changes.notify()

    def _terminated(self, _connection: object) -> None:
        self._lost.set()
        self._changes.notify()
