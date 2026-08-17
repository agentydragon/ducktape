"""Reading a conversation from a position, for a consumer that keeps its own.

A conversation is an ordered stream of `session_events` rows addressed by `event_seq` — a global
`Identity`, so one conversation's rows are not contiguous: every read is "everything after N",
never "the next one after N", and a gap is undetectable by construction rather than being a loss to
notice. A **subscription** is one consumer reading that stream from a position.

**Where the position lives is the subscriber's business, and it is not this layer's** (operator,
2026-08-17). A browser tab holds no copy that outlives it: several tabs can watch one conversation
at different points, and persisting any of those would be storing rows for something a refresh
destroys — so a tab's position is the read's own argument and the console keeps nothing
(`ClientHeldCursor`). A Matrix room holds a federated copy that outlives every console process, so
after a restart the channel has to know what it already put in the room — its position is durable,
and it lives in the channel's own storage below the channel boundary
(<channels/matrix/room_subscription.py>).

So what is shared is this interface and the read behind it. There is deliberately **no
`conversation_cursor` table**: durability is one `Cursor` implementation's concern, not a property
of subscribing.

**Read, then keep.** `Subscription.read` never advances the position. A subscriber keeps its new
one once it has done whatever the events oblige it to do — the discipline `delivery_log.retire`
already follows — so a crash in that window replays rather than skips. That is what makes a durable
subscriber at-least-once; a client-held one cannot skip at all, since it never asks for a position
it did not receive.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from haku.console.database_schema import Session, SessionEvent
from haku.console.x import session_events

# How many rows one read takes. A ceiling on the work a woken subscriber does in one pass rather
# than a page size: `Backlog.more` says to come straight back, so a subscriber catching up after an
# outage loops instead of holding one transaction open over the whole stream.
DEFAULT_SLICE = 200


@dataclass(frozen=True, order=True, slots=True)
class StreamPosition:
    """How far into a conversation's stream a subscriber has read.

    `event_seq` and nothing else, because that is the whole address. `START` is zero, which no row
    can carry — the sequence is an `Identity` and begins at one — so "before everything" needs no
    variant of its own.
    """

    event_seq: int


START = StreamPosition(event_seq=0)


@dataclass(frozen=True, slots=True)
class StreamedEvent:
    """One row of the stream, as a subscriber reads it.

    The kind is not carried beside `body`: the body's type *is* the kind, so a consumer dispatches
    on it with `isinstance` and cannot be handed a pair that disagrees.
    """

    position: StreamPosition
    session_id: UUID
    turn_id: UUID | None
    created_at: datetime
    body: session_events.StoredBody


@dataclass(frozen=True, slots=True)
class Backlog:
    """What this subscriber has not seen, and where taking it leaves it."""

    events: tuple[StreamedEvent, ...]
    # The last event's position, or the position read from where there were none — never the head
    # of the stream, so keeping this can only ever cover events the subscriber was actually handed.
    position: StreamPosition
    # Whether the read stopped at its limit. A subscriber that must catch up reads again at once.
    more: bool


@dataclass(frozen=True, slots=True)
class Unstarted:
    """This subscriber has no position: it has never read this conversation.

    Only a kept position can be absent — a request always carries one, even if it is `START` — so
    this is the state a durable cursor is in before its first read. Its whole content is where the
    stream is now, because what a subscriber joining a conversation already in progress does with
    the backlog is its own decision: a channel already holding a copy of it keeps the head and says
    nothing, a fresh reader starts from `START` instead.
    """

    head: StreamPosition


type Read = Backlog | Unstarted


class ConversationStream:
    """The conversation layer's half of a subscription: everything after a position.

    Keyed by the thread rather than by the session running it, which is what makes a position
    survive a session being replaced: the events of the session that died and of the one that took
    over are one stream, and reading only the live session's rows would skip whatever its
    predecessor wrote after the subscriber's position.
    """

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def read(self, conversation_id: UUID, *, after: StreamPosition, limit: int = DEFAULT_SLICE) -> Backlog:
        async with self._sessions() as db:
            rows = (
                await db.scalars(
                    select(SessionEvent)
                    .join(Session, Session.session_id == SessionEvent.session_id)
                    .where(Session.conversation_id == conversation_id, SessionEvent.event_seq > after.event_seq)
                    .order_by(SessionEvent.event_seq)
                    # One past the limit, so "there is more" is read rather than guessed from a
                    # full page — which would be wrong exactly when the stream ends on one.
                    .limit(limit + 1)
                )
            ).all()
        events = tuple(_streamed(row) for row in rows[:limit])
        return Backlog(events=events, position=events[-1].position if events else after, more=len(rows) > limit)

    async def head(self, conversation_id: UUID) -> StreamPosition:
        """Where this conversation's stream has got to — the position a subscriber joining it now
        would already be caught up at."""
        async with self._sessions() as db:
            highest: int | None = await db.scalar(
                select(func.max(SessionEvent.event_seq))
                .select_from(SessionEvent)
                .join(Session, Session.session_id == SessionEvent.session_id)
                .where(Session.conversation_id == conversation_id)
            )
        return StreamPosition(event_seq=highest or START.event_seq)


def _streamed(row: SessionEvent) -> StreamedEvent:
    return StreamedEvent(
        position=StreamPosition(event_seq=row.event_seq),
        session_id=row.session_id,
        turn_id=row.turn_id,
        created_at=row.created_at,
        body=session_events.body_of(row),
    )


class Cursor(Protocol):
    """Where one subscriber's position is kept.

    The one thing implementations differ in, which is why it is the only port here: reading the
    stream is shared, keeping a place in it is not.
    """

    async def position(self) -> StreamPosition | None: ...

    async def keep(self, position: StreamPosition) -> None: ...


@dataclass(frozen=True, slots=True)
class ClientHeldCursor:
    """The position a subscriber carries itself: the read's own argument, and no server state.

    This is the SPA's form — `?after=N` on the request, with the answer's own position going back
    in the response. Several tabs can watch one conversation at once, each at a different point,
    and the console stores nothing about any of them; that is the whole reason the position is a
    parameter rather than a row.

    Never `Unstarted`: a request that names no position names `START`, which is a position.
    """

    after: StreamPosition

    async def position(self) -> StreamPosition:
        return self.after

    async def keep(self, position: StreamPosition) -> None:
        """Nothing is kept, and that is the implementation rather than an omission.

        The position went back with the events that produced it; where it is kept is the client's
        own memory, and its next request is the only place it reappears.
        """


@dataclass(frozen=True, slots=True)
class Subscription:
    """One consumer reading one conversation from a position it owns."""

    stream: ConversationStream
    cursor: Cursor
    conversation_id: UUID

    async def read(self, *, limit: int = DEFAULT_SLICE) -> Read:
        if (position := await self.cursor.position()) is None:
            return Unstarted(head=await self.stream.head(self.conversation_id))
        return await self.stream.read(self.conversation_id, after=position, limit=limit)

    async def keep(self, position: StreamPosition) -> None:
        await self.cursor.keep(position)
