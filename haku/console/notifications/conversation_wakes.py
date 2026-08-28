"""The conversation layer's Postgres wake channel and its registration surface.

`conversation_wakes` carries `ConversationWakeEvent {kind, conversation_id, position}` for
conversation subscribers — the follow socket, the Matrix subscriber, the runtime supervisor, the
console-tab fan-out. Nothing session-shaped travels here or reaches a consumer: a conversation and
a session sit at different layers, so their wakes do not share a wire, a connection, or a module. A
consumer handed this surface names a conversation-shaped type that can reach nothing
session-shaped — <../docs/chat_layers.md>'s grep test made structural rather than reviewed.

The channel is named `conversation_wakes`, not `conversation_events`: what travels on it is a
level-triggered wake, while `conversation_event` is the durable record's own vocabulary, and one
name for both would suggest the record rides the notification. Nothing does — a wake says to look,
and the record stays the authority.

`ConversationWakes` owns its own `WakeListener` on this channel (`../pg_wake.py`), so a reconnect
gap here rechecks conversation watchers alone. **Registration is by scope, never by kind:** a
consumer registers for the channel as a whole and receives every kind there as payload to dispatch
on itself.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from haku.console.notifications.pg_wake import WakeListener, notify_raw
from haku.console.notifications.session_wakes import SessionEventKind, notify
from util.enum_vocab import UnknownValue, member_or_unknown

CHANNEL = "conversation_wakes"


class ConversationWakeKind(StrEnum):
    RUNTIME_DEMAND = "runtime_demand"
    """The conversation has queued work and may need a session to run it."""

    UPDATE = "update"
    """The conversation's record or its live session's view changed: subscribers should re-read."""


class ConversationWakeEvent(BaseModel):
    """What travels on `CHANNEL`, and what a conversation watcher is handed.

    The same cross-replica wire contract as `session_wakes.SessionEvent`, for the same reasons: add
    fields, never rename or remove one; unknown fields are ignored; an unknown kind is delivered as
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

    Never on the wire — synthesized by `ConversationWakes._recheck` when the listener reconnects
    over a gap: the notifications committed while the socket was down are gone, so "look at
    everything" is the only correct wake.
    """


async def notify_conversation(
    db: AsyncSession, kind: ConversationWakeKind, conversation_id: UUID, *, position: int | None = None
) -> None:
    """Emit a conversation wake inside the caller's transaction, so it fires on commit.

    *position* is the optional read-skip hint (`ConversationWakeEvent.position`); omitting it is
    always correct.
    """
    await notify_raw(
        db,
        CHANNEL,
        ConversationWakeEvent(kind=kind, conversation_id=conversation_id, position=position).model_dump_json(),
    )


async def notify_update(
    db: AsyncSession, *, session_id: UUID, conversation_id: UUID, position: int | None = None
) -> None:
    """Both layers' wakes for one write.

    Every write that changes what a conversation shows owes the session view its invalidation and
    the conversation's subscribers their wake; one call keeps a future write site from forgetting
    half of that. It lives at the conversation layer because that is the higher one — a
    conversation-visible change is the trigger, and reaching down to invalidate the underlying
    session view (`session_wakes.notify`) is the allowed direction.
    """
    await notify(db, SessionEventKind.UPDATE, session_id)
    await notify_conversation(db, ConversationWakeKind.UPDATE, conversation_id, position=position)


class ConversationWakes:
    """The conversation layer's registration surface: `watch`.

    The shape every conversation-scoped consumer registers with. Nothing session-shaped reaches the
    callback: it is handed each conversation wake as the payload or `RecheckHeld`, and dispatches on
    the two variants — a wake naming its conversation, or "re-check whatever you hold", which the
    wire never carries and a reconnect synthesizes. It owns its own `WakeListener` on
    `conversation_wakes`, which parses each payload into a `ConversationWakeEvent` and drives
    `_deliver`; a reconnect on that connection drives `_recheck`.
    """

    def __init__(self, database_url: str) -> None:
        self._watchers: set[Callable[[ConversationWakeEvent | RecheckHeld], None]] = set()
        self._listener = WakeListener(
            database_url, CHANNEL, ConversationWakeEvent, self._deliver, self._recheck, task_name="conversation-wakes"
        )

    async def start(self) -> None:
        await self._listener.start()

    async def aclose(self) -> None:
        await self._listener.aclose()

    @contextmanager
    def watch(self, on_wake: Callable[[ConversationWakeEvent | RecheckHeld], None]) -> Iterator[None]:
        """Hand *on_wake* every conversation wake, as the payload or `RecheckHeld`.

        Nothing session-shaped reaches the callback, and the consumer dispatches on the two
        variants — a wake naming its conversation, or "re-check whatever you hold", which the wire
        never carries and a reconnect synthesizes.

        The callback runs on asyncpg's reader task: record the wake and do the work elsewhere.
        """
        self._watchers.add(on_wake)
        try:
            yield
        finally:
            self._watchers.discard(on_wake)

    def _deliver(self, wake: ConversationWakeEvent) -> None:
        """Fan one parsed `conversation_wakes` payload out to every conversation watcher.

        Called by the `WakeListener` on asyncpg's reader task, so this must not block or await.
        """
        for watcher in self._watchers:
            watcher(wake)

    def _recheck(self) -> None:
        """On a listener reconnect, send every watcher `RecheckHeld`: the notifications committed
        during the gap are gone, so its only correct answer is "look at everything you hold"."""
        for watcher in self._watchers:
            watcher(RecheckHeld())
