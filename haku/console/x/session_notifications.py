"""The chat runtime's Postgres LISTEN/NOTIFY wake channels.

Separate from `SessionStore` because it is not storage: a repository answers questions about rows,
and this wakes tasks.

**Deviation from a pooled connection:** one long-lived connection with a reconnect loop, rather
than borrowing from the SQLAlchemy pool per wait. Two problems go with the pooled shape — a
listener that dies takes its waiters with it, and a session-lifetime watcher holds a pool
connection for as long as it lives.

The driver is asyncpg, the same one the application's engine uses, so nothing in the console's
async path speaks two dialects. (psycopg remains for synchronous Alembic; see
<../database_migrate.py>.)

The notify half stays inside the caller's transaction (see `_notify_on`), because `pg_notify`
delivers on commit: emitting it anywhere else would announce work that a rollback then un-did.

**One channel per layer, a typed payload on each.** A session and a conversation sit at different
layers, so their wakes do not share a wire: `CHANNEL` carries `SessionEvent {kind, session_id}`
for the runtime's own consumers — a runner waiting about *its* session, the allocator, the SPA's
session-view invalidation — and `CONVERSATION_CHANNEL` carries `ConversationWakeEvent
{kind, conversation_id, position}` for conversation subscribers, which never see a session id.
The kind travels in the payload rather than the channel name, which would need another LISTEN per
kind; `pg_notify` allows 8000 bytes and these use around a hundred.

The conversation channel is named `conversation_wakes`, not `conversation_events`: what travels on
it is a level-triggered wake, while `conversation_event` is the durable record's own vocabulary,
and one name for both would suggest the record rides the notification. Nothing does — a wake says
to look, and the record stays the authority.

**Registration is by scope, never by kind.** A consumer registers for its session, or for the
conversation channel as a whole, and receives every kind there as payload to dispatch on itself.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from typing import Any
from uuid import UUID

import asyncpg
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession

from util.enum_vocab import UnknownValue, member_or_unknown

logger = logging.getLogger(__name__)

CHANNEL = "session_events"
CONVERSATION_CHANNEL = "conversation_wakes"


class SessionEventKind(StrEnum):
    PROMPT = "prompt"
    """A prompt is queued: whichever replica runs this session's turn loop should pick it up."""

    UPDATE = "update"
    """Session or message rows changed: readers should re-read the session view."""

    ABORT = "abort"
    """The operator asked for the in-flight turn to be interrupted."""


class ConversationWakeKind(StrEnum):
    RUNTIME_DEMAND = "runtime_demand"
    """The conversation has queued work and may need a session to run it."""

    UPDATE = "update"
    """The conversation's record or its live session's view changed: subscribers should re-read."""


class SessionEvent(BaseModel):
    """What travels on `CHANNEL`.

    A cross-replica wire contract: both ends of a notification are separate pods, which may run
    different releases during a roll. Add fields, never rename or remove one, and treat a change of
    the channel itself as destructive — see the expand/contract note in the README.

    **So unknown fields are ignored and an unknown kind is a value rather than a parse failure.**
    Forbidding either makes "add fields" false: under `extra="forbid"` a field the next release adds
    costs the previous one every wake on this channel, including the kinds it does understand, and a
    dropped wake is a turn nobody picks up. A kind this release does not have is delivered as
    `UnknownValue`, which every consumer's own kind dispatch must pass over explicitly — waking on
    one is safe, because every wake means "re-check", never "act".
    """

    kind: SessionEventKind | UnknownValue
    session_id: UUID = Field(description="The session the wake is about. Every kind here names one.")

    # TODO: this decorator/classmethod/return quartet is duplicated on `ConversationWakeEvent` for
    #   its own enum. An `Annotated[Kind | UnknownValue, BeforeValidator(partial(member_or_unknown,
    #   Kind))]` alias minted once per enum should collapse both validators into the field's type.
    @field_validator("kind", mode="before")
    @classmethod
    def _a_kind_from_a_newer_release_is_a_value(cls, value: object) -> object:
        return member_or_unknown(SessionEventKind, value)


class ConversationWakeEvent(BaseModel):
    """What travels on `CONVERSATION_CHANNEL`, and what a conversation watcher is handed.

    The same cross-replica wire contract as `SessionEvent`, for the same reasons: add fields, never
    rename or remove one; unknown fields are ignored; an unknown kind is delivered as
    `UnknownValue` rather than failing the parse.
    """

    kind: ConversationWakeKind | UnknownValue
    conversation_id: UUID = Field(description="The conversation the wake is about.")
    position: int | None = Field(
        default=None,
        description="A hint, never a protocol: the conversation's log head (`event_seq`) as of the"
        " emitting transaction, so a subscriber already at or past it may skip a redundant read."
        " Ids and positions only — content never rides a wake, and the record stays the authority."
        " Absent when the emitting write does not know its head; consumers must stay correct"
        " treating it as absent, because wakes are lossy and coalesce.",
    )

    @field_validator("kind", mode="before")
    @classmethod
    def _a_kind_from_a_newer_release_is_a_value(cls, value: object) -> object:
        return member_or_unknown(ConversationWakeKind, value)


@dataclass(frozen=True, slots=True)
class RecheckHeld:
    """A wake naming no conversation: re-check every conversation you hold.

    Never on the wire — synthesized by `_wake_everyone` when the listener reconnects over a gap:
    the notifications committed while the socket was down are gone, so "look at everything" is the
    only correct wake.
    """


_RECONNECT_DELAY = timedelta(seconds=2)
_CONNECT_TIMEOUT_SECONDS = 10
_CLOSE_TIMEOUT_SECONDS = 2


async def notify_raw(db: AsyncSession, channel: str, payload: str) -> None:
    """One `pg_notify` inside the caller's transaction, so it fires on commit.

    The single emission both typed wakes serialize into. Tests drive it directly with payloads the
    typed emitters cannot produce — a garbled envelope, a kind from a release that does not exist.
    """
    await db.execute(text("SELECT pg_notify(:channel, :payload)"), {"channel": channel, "payload": payload})


async def notify(db: AsyncSession, kind: SessionEventKind, session_id: UUID) -> None:
    """Emit a session wake inside the caller's transaction, so it fires on commit."""
    await notify_raw(db, CHANNEL, SessionEvent(kind=kind, session_id=session_id).model_dump_json())


async def notify_conversation(
    db: AsyncSession, kind: ConversationWakeKind, conversation_id: UUID, *, position: int | None = None
) -> None:
    """Emit a conversation wake inside the caller's transaction, so it fires on commit.

    *position* is the optional read-skip hint (`ConversationWakeEvent.position`); omitting it is
    always correct.
    """
    await notify_raw(
        db,
        CONVERSATION_CHANNEL,
        ConversationWakeEvent(kind=kind, conversation_id=conversation_id, position=position).model_dump_json(),
    )


async def notify_update(
    db: AsyncSession, *, session_id: UUID, conversation_id: UUID, position: int | None = None
) -> None:
    """Both layers' wakes for one write.

    Every write that changes what a conversation shows owes the session view its invalidation and
    the conversation's subscribers their wake; one call keeps a future write site from forgetting
    half of that.
    """
    await notify(db, SessionEventKind.UPDATE, session_id)
    await notify_conversation(db, ConversationWakeKind.UPDATE, conversation_id, position=position)


def _parse[M: BaseModel](model: type[M], payload: str) -> M | None:
    try:
        return model.model_validate_json(payload)
    except ValueError:
        # Pydantic's ValidationError is a ValueError. Not raised onward: this runs on asyncpg's
        # reader task, and one bad payload must not cost the connection every other consumer is
        # being woken through.
        logger.exception("wake notification carried an unreadable payload: %r", payload)
        return None


def _terminator(terminated: asyncio.Event) -> Callable[[object], None]:
    """Bind the event per connection; a bare lambda in the loop would close over the last one."""
    return lambda _connection: terminated.set()


@contextmanager
def _registered[C](registry: defaultdict[UUID, set[C]], key: UUID, entry: C) -> Iterator[None]:
    """Hold *entry* under *key* for the duration, dropping a key left with no entries."""
    registry[key].add(entry)
    try:
        yield
    finally:
        entries = registry[key]
        entries.discard(entry)
        if not entries:
            del registry[key]


def libpq_dsn(database_url: str) -> str:
    """Drop the SQLAlchemy driver suffix, which a direct driver connection does not take."""
    return make_url(database_url).set(drivername="postgresql").render_as_string(hide_password=False)


class SessionNotifications:
    """One LISTEN connection over both wake channels, fanned out to waiters and watchers.

    A waiter (`wait`) is woken about the one session it named, by any kind; a watcher
    (`watch_session`, `watch`, `watch_conversations`) is handed each event whole and dispatches on
    its kind itself. Session wakes and conversation wakes never cross: each channel's payload names
    its own layer and reaches only that layer's registrations.
    """

    def __init__(self, database_url: str):
        self._dsn = libpq_dsn(database_url)
        self._waiters: defaultdict[UUID, set[asyncio.Event]] = defaultdict(set)
        self._session_watchers: defaultdict[UUID, set[Callable[[SessionEvent], None]]] = defaultdict(set)
        self._watchers: set[Callable[[SessionEvent], None]] = set()
        self._conversation_watchers: set[Callable[[ConversationWakeEvent | RecheckHeld], None]] = set()
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

    async def wait(self, session_id: UUID, *, timeout_seconds: float) -> bool:
        """Block until this session gets any wake. False on timeout.

        Any kind, deliberately: a waiter re-checks the durable state it is waiting on, so a wake it
        did not need costs one query. The idle prompt wait is the caller this is shaped for.
        """
        event = asyncio.Event()
        with _registered(self._waiters, session_id, event):
            try:
                async with asyncio.timeout(timeout_seconds):
                    await event.wait()
                    return True
            except TimeoutError:
                return False

    @contextmanager
    def watch_session(self, session_id: UUID, on_event: Callable[[SessionEvent], None]) -> Iterator[None]:
        """Hand *on_event* every event naming *session_id*, to dispatch on the kind itself.

        For a consumer that a plain wake cannot serve because the fact it waits for is
        edge-triggered — the abort watcher, which has no row to re-check.

        It runs on asyncpg's reader task, like the dispatch itself, so it must neither block nor
        await: record what arrived and do the work elsewhere.

        **Gotcha:** a reconnect cannot be replayed to a watcher — the events notified during the
        gap are simply gone, and there is no kind to synthesize. An edge a watcher misses is an
        edge its producer must be prepared to repeat; the operator pressing abort again is that
        recovery here.
        """
        with _registered(self._session_watchers, session_id, on_event):
            yield

    @contextmanager
    def watch(self, on_event: Callable[[SessionEvent], None]) -> Iterator[None]:
        """Hand *on_event* every session event this replica receives, whatever session it names.

        For a consumer with no session in mind — the console-socket fan-out and the allocator have
        to hear about sessions nobody has told them to expect. The same reader-task and
        no-reconnect-replay rules as `watch_session` apply; both consumers are sweeps a missed
        event only delays.
        """
        self._watchers.add(on_event)
        try:
            yield
        finally:
            self._watchers.discard(on_event)

    @contextmanager
    def watch_conversations(self, on_wake: Callable[[ConversationWakeEvent | RecheckHeld], None]) -> Iterator[None]:
        """Hand *on_wake* every conversation wake, as the payload or `RecheckHeld`.

        The shape a conversation-scoped consumer registers with: nothing session-shaped reaches
        the callback, and the consumer dispatches on the two variants — a wake naming its
        conversation, or "re-check whatever you hold", which the wire never carries and a
        reconnect synthesizes.

        The callback runs on asyncpg's reader task: record the wake and do the work elsewhere.
        """
        self._conversation_watchers.add(on_wake)
        try:
            yield
        finally:
            self._conversation_watchers.discard(on_wake)

    def _wake_everyone(self) -> None:
        """Notifications committed while reconnecting are gone; make every consumer re-check.

        A waiter re-checks the durable state it waits on, and a conversation watcher is sent
        `RecheckHeld` — its named variant of the same answer. Session watchers get nothing: there
        is no kind to synthesize, which is their documented gotcha.
        """
        for events in self._waiters.values():
            for event in events:
                event.set()
        for conversation_watcher in self._conversation_watchers:
            conversation_watcher(RecheckHeld())

    def _on_session_notification(self, _connection: object, _pid: int, _channel: str, payload: object) -> None:
        """asyncpg dispatches on its reader task, so this must not block or await.

        The parameter types are asyncpg's, not ours: its `_Listener` protocol declares the
        payload as `object` and the connection as a union with the pool proxy, so narrowing
        either here would stop matching.
        """
        event = _parse(SessionEvent, str(payload))
        if event is None:
            return
        for waiter in self._waiters.get(event.session_id, ()):
            waiter.set()
        for session_watcher in self._session_watchers.get(event.session_id, ()):
            session_watcher(event)
        for watcher in self._watchers:
            watcher(event)

    def _on_conversation_notification(self, _connection: object, _pid: int, _channel: str, payload: object) -> None:
        """The conversation channel's `_on_session_notification`, under the same reader-task rules."""
        event = _parse(ConversationWakeEvent, str(payload))
        if event is None:
            return
        for conversation_watcher in self._conversation_watchers:
            conversation_watcher(event)

    async def _listen_loop(self) -> None:
        while True:
            connection: asyncpg.Connection[Any] | None = None
            try:
                connection = await asyncpg.connect(self._dsn, timeout=_CONNECT_TIMEOUT_SECONDS)
                terminated = asyncio.Event()
                connection.add_termination_listener(_terminator(terminated))
                await connection.add_listener(CHANNEL, self._on_session_notification)
                await connection.add_listener(CONVERSATION_CHANNEL, self._on_conversation_notification)
                self._listening.set()
                self._wake_everyone()
                await terminated.wait()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Logged rather than raised: a dropped listener must not end the loop, or
                # every waiter in the process silently stops being woken.
                logger.exception("wake notification listener failed; reconnecting")
            finally:
                self._listening.clear()
                if connection is not None:
                    with suppress(Exception):
                        await connection.close(timeout=_CLOSE_TIMEOUT_SECONDS)
            await asyncio.sleep(_RECONNECT_DELAY.total_seconds())
