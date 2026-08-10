"""The chat surfaces' Postgres LISTEN/NOTIFY channel.

Separate from `ClaudeChatStore` because it is not storage: a repository answers questions
about rows, and this wakes tasks. Keeping the two in one class is what let the listener be
written against psycopg3's API while running on an asyncpg engine — it raised on every call
in production, killing every Matrix session about four seconds in, and the only test
covering it passed against a fake engine.

**Deviation from a pooled connection:** one long-lived connection with a reconnect loop,
rather than borrowing from the SQLAlchemy pool per wait. Two problems go with the pooled
shape — a listener that dies takes its waiters with it, and a session-lifetime watcher holds
a pool connection for as long as it lives.

The driver is asyncpg, the same one the application's engine uses, so nothing in the console's
async path speaks two dialects. (psycopg remains for synchronous Alembic; see
<../database_migrate.py>.)

The notify half stays inside the caller's transaction (see `notify`), because `pg_notify`
delivers on commit: emitting it anywhere else would announce work that a rollback then
un-did.

**One channel, a typed payload.** What used to be three channels carrying a bare session id
is one channel carrying a `ChatEvent`. Three channels made the event kind implicit in the
channel name — untyped, unvalidated, and impossible to extend without another LISTEN — while
`pg_notify` allows 8000 bytes of payload and this uses about seventy.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager, suppress
from datetime import timedelta
from enum import StrEnum
from typing import Any
from uuid import UUID

import asyncpg
from pydantic import BaseModel, ConfigDict
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

CHANNEL = "claude_chat"


class ChatEventKind(StrEnum):
    PROMPT = "prompt"
    """A prompt is queued: whichever replica runs this session's turn loop should pick it up."""

    UPDATE = "update"
    """Session or message rows changed: readers should re-read the session view."""

    ABORT = "abort"
    """The operator asked for the in-flight turn to be interrupted."""


class ChatEvent(BaseModel):
    """What travels on `CHANNEL`. A wire contract between replicas — see the rollout note below."""

    model_config = ConfigDict(extra="forbid")

    kind: ChatEventKind
    session_id: UUID


# CLEANUP(added 2026-08-10): drop the legacy channels, and the second `pg_notify` in `notify`,
#   once no replica predating the merge is running — i.e. one full release after this lands
#   (`kubectl get pods -n haku-console -o jsonpath='{..image}'` shows only images at or after
#   it). Renaming a channel is a destructive wire change and the Deployment rolls with
#   `maxUnavailable: 0`, so the two schemes have to overlap for one release.
_LEGACY_CHANNELS = {
    ChatEventKind.PROMPT: "claude_chat_prompt",
    ChatEventKind.UPDATE: "claude_chat_update",
    ChatEventKind.ABORT: "claude_chat_abort",
}
_LEGACY_KINDS = {channel: kind for kind, channel in _LEGACY_CHANNELS.items()}

_CHANNELS = (CHANNEL, *_LEGACY_CHANNELS.values())
_RECONNECT_DELAY = timedelta(seconds=2)
_CONNECT_TIMEOUT_SECONDS = 10
_CLOSE_TIMEOUT_SECONDS = 2


async def notify(db: AsyncSession, kind: ChatEventKind, session_id: UUID) -> None:
    """Emit `pg_notify` inside the caller's transaction, so it fires on commit."""
    await _pg_notify(db, CHANNEL, ChatEvent(kind=kind, session_id=session_id).model_dump_json())
    # Also on the pre-merge channel, so replicas still running the old code hear this. See
    # the tombstone above for when this line goes.
    await _pg_notify(db, _LEGACY_CHANNELS[kind], str(session_id))


async def _pg_notify(db: AsyncSession, channel: str, payload: str) -> None:
    await db.execute(text("SELECT pg_notify(:channel, :payload)"), {"channel": channel, "payload": payload})


def _parse(channel: str, payload: str) -> ChatEvent | None:
    try:
        if channel == CHANNEL:
            return ChatEvent.model_validate_json(payload)
        return ChatEvent(kind=_LEGACY_KINDS[channel], session_id=UUID(payload))
    except ValueError:
        # Pydantic's ValidationError is a ValueError, as is a malformed UUID. Neither is
        # raised onward: this runs on asyncpg's reader task, and one bad payload must not
        # cost the connection every other session is being woken through.
        logger.exception("chat notification on %s carried an unreadable payload: %r", channel, payload)
        return None


def _terminator(terminated: asyncio.Event) -> Callable[[object], None]:
    """Bind the event per connection; a bare lambda in the loop would close over the last one."""
    return lambda _connection: terminated.set()


def libpq_dsn(database_url: str) -> str:
    """Drop the SQLAlchemy driver suffix, which a direct driver connection does not take."""
    return make_url(database_url).set(drivername="postgresql").render_as_string(hide_password=False)


class ChatNotifications:
    """One LISTEN connection over the chat channel, fanned out to per-session waiters."""

    def __init__(self, database_url: str):
        self._dsn = libpq_dsn(database_url)
        self._waiters: dict[tuple[ChatEventKind, UUID], set[asyncio.Event]] = {}
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
    def _registered(self, kind: ChatEventKind, session_id: UUID) -> Iterator[asyncio.Event]:
        event = asyncio.Event()
        key = (kind, session_id)
        self._waiters.setdefault(key, set()).add(event)
        try:
            yield event
        finally:
            waiters = self._waiters.get(key)
            if waiters is not None:
                waiters.discard(event)
                if not waiters:
                    del self._waiters[key]

    async def wait(self, kind: ChatEventKind, session_id: UUID, *, timeout_seconds: float) -> bool:
        """Block until this session gets a *kind* event. False on timeout."""
        with self._registered(kind, session_id) as event:
            try:
                async with asyncio.timeout(timeout_seconds):
                    await event.wait()
                    return True
            except TimeoutError:
                return False

    @asynccontextmanager
    async def subscribe(self, kind: ChatEventKind, session_id: UUID) -> AsyncIterator[asyncio.Event]:
        """Hold a registration for as long as the caller needs it.

        For watchers that outlive a single wait: the caller clears the event and waits
        again. Registration is in-process, so unlike a per-wait connection there is no
        window between waits in which a notification is lost.
        """
        with self._registered(kind, session_id) as event:
            yield event

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
        event = _parse(channel, str(payload))
        if event is None:
            return
        for waiter in self._waiters.get((event.kind, event.session_id), ()):
            waiter.set()

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
