"""Commit wakeups for bounded Action waits; PostgreSQL remains the state authority.

One dedicated LISTEN connection fans out UUID-only invalidations to this process's waiters.
Unlike console session followers, these short-lived waits fail explicitly on channel loss:
there is no best-effort startup, reconnect delay, or state-polling fallback.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from uuid import UUID

import asyncpg
from sqlalchemy.engine import make_url

CHANNEL = "agentplane_action_updates"
logger = logging.getLogger(__name__)


class UpdatesUnavailableError(Exception):
    """The caller can recover its receipt with an immediate read, without resubmission."""


class ActionUpdates:
    def __init__(self, database_url: str) -> None:
        self._dsn = make_url(database_url).set(drivername="postgresql").render_as_string(hide_password=False)
        self._connection: asyncpg.Connection[Any] | None = None
        self._subscribers: dict[UUID, set[asyncio.Event]] = {}
        self._available = False

    async def start(self) -> None:
        if self._connection is not None:
            raise RuntimeError("Action update listener already started")
        self._connection = await asyncpg.connect(self._dsn, timeout=10)
        self._connection.add_termination_listener(self._terminated)
        try:
            await self._connection.add_listener(CHANNEL, self._notified)
            self._available = not self._connection.is_closed()
            self.check_available()
        except BaseException:
            await self.close()
            raise

    async def close(self) -> None:
        self._terminated(None)
        if self._connection is not None:
            await self._connection.close(timeout=2)
            self._connection = None

    def check_available(self) -> None:
        if not self._available:
            raise UpdatesUnavailableError(
                "Action update channel unavailable; read the existing request with wait_seconds=0. "
                "Do not submit a new idempotency key."
            )

    @contextmanager
    def subscribe(self, request_id: UUID) -> Iterator[asyncio.Event]:
        self.check_available()
        changed = asyncio.Event()
        subscribers = self._subscribers.setdefault(request_id, set())
        subscribers.add(changed)
        try:
            yield changed
        finally:
            subscribers.remove(changed)
            if not subscribers:
                del self._subscribers[request_id]

    def _notified(self, _connection: object, _pid: int, _channel: str, payload: object) -> None:
        try:
            request_id = UUID(str(payload))
        except ValueError:
            # An unreadable invalidation could name any waiter. Re-read them all rather than
            # silently losing an update or trusting notification text as durable state.
            logger.warning("Unreadable Action invalidation; waking all subscribers to re-read durable state")
            for subscribers in self._subscribers.values():
                for changed in subscribers:
                    changed.set()
            return
        for changed in self._subscribers.get(request_id, ()):
            changed.set()

    def _terminated(self, _connection: object) -> None:
        self._available = False
        for subscribers in self._subscribers.values():
            for changed in subscribers:
                changed.set()
