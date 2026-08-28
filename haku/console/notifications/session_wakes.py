"""The session layer's Postgres wake channel and its registration surface.

`session_events` carries `SessionEvent {kind, session_id}` for the runtime's own consumers — a
runner waiting about *its* session, the allocator hearing about any session. Nothing
conversation-shaped travels here or reaches a consumer: a session and a conversation sit at
different layers, so their wakes do not share a wire, a connection, or a module. The kind travels
in the payload rather than the channel name, which would need another LISTEN per kind; `pg_notify`
allows 8000 bytes and these use around a hundred.

`SessionWakes` owns its own `WakeListener` on this channel (`../pg_wake.py`), so a reconnect gap
here rechecks session waiters alone. **Registration is by scope, never by kind:** a consumer
registers for its session, or for the channel as a whole, and receives every kind there as payload
to dispatch on itself.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from haku.console.notifications.pg_wake import WakeListener, notify_raw
from util.enum_vocab import UnknownValue, member_or_unknown

CHANNEL = "session_events"


class SessionEventKind(StrEnum):
    PROMPT = "prompt"
    """A prompt is queued: whichever replica runs this session's turn loop should pick it up."""

    UPDATE = "update"
    """Session or message rows changed: readers should re-read the session view."""

    ABORT = "abort"
    """The operator asked for the in-flight turn to be interrupted."""


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

    @field_validator("kind", mode="before")
    @classmethod
    def _a_kind_from_a_newer_release_is_a_value(cls, value: object) -> object:
        return member_or_unknown(SessionEventKind, value)


async def notify(db: AsyncSession, kind: SessionEventKind, session_id: UUID) -> None:
    """Emit a session wake inside the caller's transaction, so it fires on commit."""
    await notify_raw(db, CHANNEL, SessionEvent(kind=kind, session_id=session_id).model_dump_json())


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


class SessionWakes:
    """The session layer's registration surface: `wait`, `watch_session`, `watch`.

    A waiter (`wait`) is woken about the one session it named, by any kind; a watcher
    (`watch_session`, `watch`) is handed each event whole and dispatches on its kind itself. Nothing
    conversation-shaped reaches here. It owns its own `WakeListener` on `session_events`, which
    parses each payload into a `SessionEvent` and drives `_deliver`; a reconnect on that connection
    drives `_recheck`.
    """

    def __init__(self, database_url: str) -> None:
        self._waiters: defaultdict[UUID, set[asyncio.Event]] = defaultdict(set)
        self._session_watchers: defaultdict[UUID, set[Callable[[SessionEvent], None]]] = defaultdict(set)
        self._watchers: set[Callable[[SessionEvent], None]] = set()
        self._listener = WakeListener(
            database_url, CHANNEL, SessionEvent, self._deliver, self._recheck, task_name="session-wakes"
        )

    async def start(self) -> None:
        await self._listener.start()

    async def aclose(self) -> None:
        await self._listener.aclose()

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

        For a consumer with no session in mind — the allocator has to hear about sessions nobody
        has told it to expect. The same reader-task and no-reconnect-replay rules as `watch_session`
        apply; the allocator is a sweep a missed event only delays.
        """
        self._watchers.add(on_event)
        try:
            yield
        finally:
            self._watchers.discard(on_event)

    def _deliver(self, event: SessionEvent) -> None:
        """Fan one parsed `session_events` payload out to waiters and watchers.

        Called by the `WakeListener` on asyncpg's reader task, so this must not block or await.
        """
        for waiter in self._waiters.get(event.session_id, ()):
            waiter.set()
        for session_watcher in self._session_watchers.get(event.session_id, ()):
            session_watcher(event)
        for watcher in self._watchers:
            watcher(event)

    def _recheck(self) -> None:
        """On a listener reconnect, wake every waiter to re-read its own durable state.

        Session watchers get nothing: there is no kind to synthesize, which is their documented
        gotcha (`watch_session`).
        """
        for events in self._waiters.values():
            for event in events:
                event.set()
