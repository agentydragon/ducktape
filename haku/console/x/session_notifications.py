"""The session runtime's Postgres LISTEN/NOTIFY channel.

Separate from `SessionStore` because it is not storage: a repository answers questions about rows,
and this wakes tasks.

**Deviation from a pooled connection:** one long-lived connection with a reconnect loop, rather
than borrowing from the SQLAlchemy pool per wait. Two problems go with the pooled shape — a
listener that dies takes its waiters with it, and a session-lifetime watcher holds a pool
connection for as long as it lives.

The driver is asyncpg, the same one the application's engine uses, so nothing in the console's
async path speaks two dialects. (psycopg remains for synchronous Alembic; see
<../database_migrate.py>.)

The notify half stays inside the caller's transaction (see `notify`), because `pg_notify` delivers
on commit: emitting it anywhere else would announce work that a rollback then un-did.

**One channel, a typed payload.** Every event travels on `CHANNEL` as a `SessionEvent` rather than
the kind being implicit in a channel name, which would need another LISTEN per kind. `pg_notify`
allows 8000 bytes of payload; this uses about a hundred.

**Every wake names its conversation.** A conversation-scoped consumer — a channel subscriber, the
follow socket — registers by `conversation_id`; only a wait about one runner incarnation (a prompt
or abort on *its* session) registers by `session_id`. The historical payload field stays spelled
`session_id` because readers that predate `conversation_id` require it to parse the payload at
all; `runtime_demand`, which has no session, places its conversation id there.
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
from pydantic import BaseModel, field_validator
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession

from util.sqlalchemy_types import UnknownValue

logger = logging.getLogger(__name__)

CHANNEL = "session_events"


class SessionEventKind(StrEnum):
    RUNTIME_DEMAND = "runtime_demand"
    """A conversation has queued work and may need a session to run it."""

    PROMPT = "prompt"
    """A prompt is queued: whichever replica runs this session's turn loop should pick it up."""

    UPDATE = "update"
    """Session or message rows changed: readers should re-read the session view."""

    ABORT = "abort"
    """The operator asked for the in-flight turn to be interrupted."""


# Membership, read once. A `StrEnum` member hashes as its own string, so this answers for the raw
# value off the wire without constructing anything.
_KINDS = frozenset(SessionEventKind)


class SessionEvent(BaseModel):
    """What travels on `CHANNEL`.

    A cross-replica wire contract: both ends of a notification are separate pods, which may run
    different releases during a roll. Add fields, never rename or remove one, and treat a change of
    `CHANNEL` itself as destructive — see the expand/contract note in the README.

    **So unknown fields are ignored and an unknown kind is a value rather than a parse failure.**
    Forbidding either makes "add fields" false: under `extra="forbid"` a field the next release adds
    costs the previous one every wake on this channel, including the kinds it does understand, and a
    dropped wake is a turn nobody picks up. A kind it does not understand legitimately wakes nobody
    — no waiter is registered under one — and saying so as `UnknownValue` is what keeps a roll
    distinguishable from a corrupt payload in the log.
    """

    kind: SessionEventKind | UnknownValue
    session_id: UUID
    """The legacy wire slot, and the session-scoped subject where one exists.

    A kind whose subject is a session names it here. ``runtime_demand`` has no session and places
    its conversation id here as well: readers that predate ``conversation_id`` require this field
    to parse the payload at all, so it is always filled.
    """

    # CLEANUP(added 2026-08-27): make `conversation_id` required (drop `| None = None`) once the
    #   deploy that writes it on every wake has converged (both haku-console replicas on an image
    #   at or after this commit); a NOTIFY payload does not outlive its delivery.
    conversation_id: UUID | None = None
    """The conversation the wake is about — what a conversation-scoped consumer keys on.

    `None` only off a payload written by a release from before this field existed.
    """

    @field_validator("kind", mode="before")
    @classmethod
    def _a_kind_from_a_newer_release_is_a_value(cls, value: object) -> object:
        return UnknownValue(value) if isinstance(value, str) and value not in _KINDS else value


_RECONNECT_DELAY = timedelta(seconds=2)
_CONNECT_TIMEOUT_SECONDS = 10
_CLOSE_TIMEOUT_SECONDS = 2


async def notify(db: AsyncSession, kind: SessionEventKind, *, session_id: UUID | None, conversation_id: UUID) -> None:
    """Emit `pg_notify` inside the caller's transaction, so it fires on commit.

    *session_id* is `None` for a wake whose subject has no session (`runtime_demand`); the wire's
    legacy `session_id` slot is then filled with the conversation id, which readers that predate
    `conversation_id` require in order to parse the payload at all.
    """
    await db.execute(
        text("SELECT pg_notify(:channel, :payload)"),
        {
            "channel": CHANNEL,
            "payload": SessionEvent(
                kind=kind,
                session_id=conversation_id if session_id is None else session_id,
                conversation_id=conversation_id,
            ).model_dump_json(),
        },
    )


def _parse(payload: str) -> SessionEvent | None:
    try:
        return SessionEvent.model_validate_json(payload)
    except ValueError:
        # Pydantic's ValidationError is a ValueError. Not raised onward: this runs on asyncpg's
        # reader task, and one bad payload must not cost the connection every other session is
        # being woken through.
        logger.exception("session notification carried an unreadable payload: %r", payload)
        return None


def _terminator(terminated: asyncio.Event) -> Callable[[object], None]:
    """Bind the event per connection; a bare lambda in the loop would close over the last one."""
    return lambda _connection: terminated.set()


@contextmanager
def _watching[C](registry: dict[SessionEventKind, set[C]], kind: SessionEventKind, callback: C) -> Iterator[None]:
    """Register *callback* under *kind* for the duration, dropping a kind left with no watchers."""
    watchers = registry.setdefault(kind, set())
    watchers.add(callback)
    try:
        yield
    finally:
        watchers.discard(callback)
        if not watchers:
            registry.pop(kind, None)


def libpq_dsn(database_url: str) -> str:
    """Drop the SQLAlchemy driver suffix, which a direct driver connection does not take."""
    return make_url(database_url).set(drivername="postgresql").render_as_string(hide_password=False)


class SessionNotifications:
    """One LISTEN connection over the session channel, fanned out to waiters and watchers.

    A waiter (`wait`, `subscribe`) is woken about the one session it named; a watcher (`watch`,
    `watch_conversations`) is handed every event of a kind, for a consumer that cannot name its
    subjects in advance.
    """

    def __init__(self, database_url: str):
        self._dsn = libpq_dsn(database_url)
        self._waiters: dict[tuple[SessionEventKind, UUID], set[asyncio.Event]] = {}
        self._watchers: dict[SessionEventKind, set[Callable[[UUID], None]]] = {}
        self._conversation_watchers: dict[SessionEventKind, set[Callable[[UUID | None], None]]] = {}
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
    def _registered(self, kind: SessionEventKind, session_id: UUID) -> Iterator[asyncio.Event]:
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

    async def wait(self, kind: SessionEventKind, session_id: UUID, *, timeout_seconds: float) -> bool:
        """Block until this session gets a *kind* event. False on timeout."""
        with self._registered(kind, session_id) as event:
            try:
                async with asyncio.timeout(timeout_seconds):
                    await event.wait()
                    return True
            except TimeoutError:
                return False

    @asynccontextmanager
    async def subscribe(self, kind: SessionEventKind, session_id: UUID) -> AsyncIterator[asyncio.Event]:
        """Hold a registration for as long as the caller needs it.

        For watchers that outlive a single wait: the caller clears the event and waits again.
        Registration is in-process, so there is no window between waits in which a notification is
        lost.
        """
        with self._registered(kind, session_id) as event:
            yield event

    @contextmanager
    def watch(self, kind: SessionEventKind, on_session: Callable[[UUID], None]) -> Iterator[None]:
        """Hand *on_session* every *kind* event this replica receives, whatever session it names.

        For a consumer with no session in mind — the console-socket fan-out has to hear about
        sessions nobody has told it to expect, and `subscribe` registers a waiter per
        `(kind, session_id)`. A callback rather than an `asyncio.Event` because the id *is* the
        payload.

        It runs on asyncpg's reader task, like `_on_notification` itself, so it must neither block
        nor await: record the id and do the work elsewhere.

        **Gotcha:** a reconnect cannot be replayed to a watcher. `_wake_everyone` tells each
        waiter to re-read the session it already knows about, and there is no equivalent here —
        the ids notified during the gap are simply gone. A watcher must therefore be something a
        missed event only delays, never something a missed event loses.
        """
        with _watching(self._watchers, kind, on_session):
            yield

    @contextmanager
    def watch_conversations(
        self, kind: SessionEventKind, on_conversation: Callable[[UUID | None], None]
    ) -> Iterator[None]:
        """Hand *on_conversation* every *kind* event's conversation id.

        The shape a conversation-scoped consumer registers with: nothing session-shaped reaches
        the callback. `None` is a wake that could not name its conversation — a payload from a
        release before the field existed, or the listener reconnecting over a gap — and means
        "re-check whatever you hold", the same answer `_wake_everyone` gives a waiter.

        Like `watch`, the callback runs on asyncpg's reader task: record the id and do the work
        elsewhere.
        """
        with _watching(self._conversation_watchers, kind, on_conversation):
            yield

    def _wake_everyone(self) -> None:
        """Notifications committed while reconnecting are gone; make every consumer re-check.

        A conversation watcher is poked with `None` — its "re-check what you hold" arm. A session
        watcher has no such arm to poke (the id *is* its payload), which is `watch`'s documented
        gotcha.
        """
        for events in self._waiters.values():
            for event in events:
                event.set()
        for conversation_watchers in self._conversation_watchers.values():
            for watcher in conversation_watchers:
                watcher(None)

    def _on_notification(self, _connection: object, _pid: int, _channel: str, payload: object) -> None:
        """asyncpg dispatches on its reader task, so this must not block or await.

        The parameter types are asyncpg's, not ours: its `_Listener` protocol declares the
        payload as `object` and the connection as a union with the pool proxy, so narrowing
        either here would stop matching.
        """
        event = _parse(str(payload))
        if event is None:
            return
        if isinstance(event.kind, UnknownValue):
            # A kind the release that emitted it has and this one does not. Nothing here is
            # registered under it, so there is nobody to wake and this is the whole handling —
            # logged at debug because it is what a roll looks like, not a fault.
            logger.debug("session notification names a kind this release does not have: %s", event.kind)
            return
        for waiter in self._waiters.get((event.kind, event.session_id), ()):
            waiter.set()
        for watcher in self._watchers.get(event.kind, ()):
            watcher(event.session_id)
        for conversation_watcher in self._conversation_watchers.get(event.kind, ()):
            conversation_watcher(event.conversation_id)

    async def _listen_loop(self) -> None:
        while True:
            connection: asyncpg.Connection[Any] | None = None
            try:
                connection = await asyncpg.connect(self._dsn, timeout=_CONNECT_TIMEOUT_SECONDS)
                terminated = asyncio.Event()
                connection.add_termination_listener(_terminator(terminated))
                await connection.add_listener(CHANNEL, self._on_notification)
                self._listening.set()
                self._wake_everyone()
                await terminated.wait()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Logged rather than raised: a dropped listener must not end the loop, or
                # every waiter in the process silently stops being woken.
                logger.exception("session notification listener failed; reconnecting")
            finally:
                self._listening.clear()
                if connection is not None:
                    with suppress(Exception):
                        await connection.close(timeout=_CLOSE_TIMEOUT_SECONDS)
            await asyncio.sleep(_RECONNECT_DELAY.total_seconds())
