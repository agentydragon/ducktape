"""The chat surfaces' Postgres LISTEN/NOTIFY channel.

Separate from `ClaudeChatStore` because it is not storage: a repository answers questions
about rows, and this wakes tasks. Keeping the two in one class is what let the listener be
written against psycopg3's API while running on an asyncpg engine — it raised on every call
in production, killing every Matrix session about four seconds in, and the only test
covering it passed against a fake engine.

**Deviation from a pooled connection:** one long-lived connection for all three channels with
a reconnect loop, rather than borrowing from the SQLAlchemy pool per wait. Two problems go
with the pooled shape — a listener that dies takes its waiters with it, and a
session-lifetime watcher holds a pool connection for as long as it lives.

The driver is asyncpg, the same one the application's engine uses, so nothing in the console's
async path speaks two dialects. (psycopg remains for synchronous Alembic; see
<../database_migrate.py>.)

The notify half stays inside the caller's transaction (see `notify`), because `pg_notify`
delivers on commit: emitting it anywhere else would announce work that a rollback then
un-did.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager, suppress
from datetime import timedelta
from typing import Any
from uuid import UUID

import asyncpg
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

PROMPT_CHANNEL = "claude_chat_prompt"
UPDATE_CHANNEL = "claude_chat_update"
ABORT_CHANNEL = "claude_chat_abort"

_CHANNELS = (PROMPT_CHANNEL, UPDATE_CHANNEL, ABORT_CHANNEL)
_RECONNECT_DELAY = timedelta(seconds=2)
_CONNECT_TIMEOUT_SECONDS = 10
_CLOSE_TIMEOUT_SECONDS = 2


async def notify(db: AsyncSession, channel: str, session_id: UUID) -> None:
    """Emit `pg_notify` inside the caller's transaction, so it fires on commit."""
    await db.execute(text("SELECT pg_notify(:channel, :payload)"), {"channel": channel, "payload": str(session_id)})


def _terminator(terminated: asyncio.Event) -> Callable[[object], None]:
    """Bind the event per connection; a bare lambda in the loop would close over the last one."""
    return lambda _connection: terminated.set()


def libpq_dsn(database_url: str) -> str:
    """Drop the SQLAlchemy driver suffix, which a direct driver connection does not take."""
    return make_url(database_url).set(drivername="postgresql").render_as_string(hide_password=False)


class ChatNotifications:
    """One LISTEN connection over the chat channels, fanned out to per-session waiters."""

    def __init__(self, database_url: str):
        self._dsn = libpq_dsn(database_url)
        self._waiters: dict[tuple[str, UUID], set[asyncio.Event]] = {}
        self._task: asyncio.Task[None] | None = None
        self._listening = asyncio.Event()

    async def start(self) -> None:
        self._task = asyncio.create_task(self._listen_loop())
        # Wait for the first LISTEN so a caller that subscribes immediately cannot miss a
        # notification emitted between construction and the socket being live.
        with suppress(TimeoutError):
            async with asyncio.timeout(_CONNECT_TIMEOUT_SECONDS):
                await self._listening.wait()

    async def aclose(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    @contextmanager
    def _registered(self, channel: str, session_id: UUID) -> Iterator[asyncio.Event]:
        event = asyncio.Event()
        key = (channel, session_id)
        self._waiters.setdefault(key, set()).add(event)
        try:
            yield event
        finally:
            waiters = self._waiters.get(key)
            if waiters is not None:
                waiters.discard(event)
                if not waiters:
                    del self._waiters[key]

    async def wait(self, channel: str, session_id: UUID, *, timeout_seconds: float) -> bool:
        """Block until this session is notified on *channel*. False on timeout."""
        with self._registered(channel, session_id) as event:
            try:
                async with asyncio.timeout(timeout_seconds):
                    await event.wait()
                    return True
            except TimeoutError:
                return False

    @asynccontextmanager
    async def subscribe(self, channel: str, session_id: UUID) -> AsyncIterator[asyncio.Event]:
        """Hold a registration for as long as the caller needs it.

        For watchers that outlive a single wait: the caller clears the event and waits
        again. Registration is in-process, so unlike a per-wait connection there is no
        window between waits in which a notification is lost.
        """
        with self._registered(channel, session_id) as event:
            yield event

    def _wake(self, channel: str, payload: str) -> None:
        try:
            session_id = UUID(payload)
        except ValueError:
            logger.warning("chat notification on %s carried a non-UUID payload: %r", channel, payload)
            return
        for event in self._waiters.get((channel, session_id), ()):
            event.set()

    def _wake_everyone(self) -> None:
        """Notifications committed while reconnecting are gone; make every waiter re-check."""
        for events in self._waiters.values():
            for event in events:
                event.set()

    def _on_notification(self, _connection: object, _pid: int, channel: str, payload: object) -> None:
        """asyncpg dispatches on its reader task, so this must not block or await.

        The parameter types are asyncpg's, not ours: its `_Listener` protocol declares the
        payload as `object` and the connection as a union with the pool proxy, so narrowing
        either here would stop matching.
        """
        self._wake(channel, str(payload))

    async def _listen_loop(self) -> None:
        while True:
            connection: asyncpg.Connection[Any] | None = None
            try:
                connection = await asyncpg.connect(self._dsn, timeout=_CONNECT_TIMEOUT_SECONDS)
                terminated = asyncio.Event()
                connection.add_termination_listener(_terminator(terminated))
                for channel in _CHANNELS:
                    await connection.add_listener(channel, self._on_notification)
                self._listening.set()
                self._wake_everyone()
                await terminated.wait()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Logged rather than raised: a dropped listener must not end the loop, or
                # every waiter in the process silently stops being woken.
                logger.exception("chat notification listener failed; reconnecting")
            finally:
                self._listening.clear()
                if connection is not None:
                    with suppress(Exception):
                        await connection.close(timeout=_CLOSE_TIMEOUT_SECONDS)
            await asyncio.sleep(_RECONNECT_DELAY.total_seconds())
