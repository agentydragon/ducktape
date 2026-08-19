"""Reading a conversation from a position, for a consumer that keeps its own.

A conversation is an ordered stream of `conversation_event` rows addressed by `event_seq`, dense
within the conversation and allocated under its own row lock. Density is what makes a position an
answer rather than a hint: a subscriber reading "everything after N" can tell a gap from an end, so
a lost row is a fact it can act on instead of one nothing can distinguish from silence. A
**subscription** is one consumer reading that stream from a position.

**Where the position lives is the subscriber's business, not this layer's.** A browser tab holds no
copy that outlives it — several tabs can watch one conversation at different points — so a tab's
position is the read's own argument and the console keeps nothing (`ClientHeldCursor`). A Matrix
room holds a federated copy that outlives every console process, so after a restart the channel has
to know what it already put in the room: its position is durable and lives in the channel's own
storage (<channels/matrix/room_subscription.py>).

What is shared is this interface and the read behind it. There is deliberately **no
`conversation_cursor` table**: durability is one `Cursor` implementation's concern, not a property
of subscribing.

**Read, then keep.** `Subscription.read` never advances the position. A subscriber keeps its new
one once it has done whatever the events oblige it to do — the discipline
<channels/matrix/revisions.py>'s `retire` already follows — so a crash in that window replays rather than skips. That is what makes a durable
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

from haku.console.database_schema import ConversationEvent
from haku.console.x import session_events

# How many rows one read takes. A ceiling on the work a woken subscriber does in one pass rather
# than a page size: `Backlog.more` says to come straight back, so a subscriber catching up after an
# outage loops instead of holding one transaction open over the whole stream.
DEFAULT_SLICE = 200


@dataclass(frozen=True, order=True, slots=True)
class StreamPosition:
    """How far into a conversation's stream a subscriber has read.

    `event_seq` and nothing else, because that is the whole address. `START` is zero, which no row
    can carry — a conversation's counter begins at one — so "before everything" needs no variant of
    its own.
    """

    event_seq: int


START = StreamPosition(event_seq=0)


@dataclass(frozen=True, slots=True)
class StreamedEvent:
    """One row of the stream, as a subscriber reads it.

    No kind beside `body`: the body's type *is* the kind, so a consumer dispatches on it with
    `isinstance` and cannot be handed a pair that disagrees.

    A row written by a **newer release than this one** arrives as `session_events.UnknownEventBody`
    rather than not arriving. It keeps its position, which is the point: the read stops at its limit
    where it should, `more` stays true where it should, and a subscriber's kept position advances
    over what it was actually handed instead of over a row that raised.
    """

    position: StreamPosition
    # Absent for a fact the conversation holds that no session has taken. A subscriber that renders
    # the stream needs neither, and one that reaches for the session is asking a question the layer
    # below it owns.
    session_id: UUID | None
    turn_id: UUID | None
    # Which item this row is about, absent on the console's own facts. A subscriber that *sends*
    # rather than renders needs it: what the Matrix channel owes its room is an item's whole prose,
    # which is the item's to answer and not this event's.
    item_id: UUID | None
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
    the backlog is its own decision.
    """

    head: StreamPosition


type Read = Backlog | Unstarted


class ConversationStream:
    """The conversation layer's half of a subscription: everything after a position.

    Keyed by the thread rather than by the session running it, so a position survives a session
    being replaced: reading only the live session's rows would skip whatever its predecessor wrote
    after the subscriber's position — which is why the address is the conversation's own counter
    rather than a join through whichever session happened to write the row.
    """

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def read(self, conversation_id: UUID, *, after: StreamPosition, limit: int = DEFAULT_SLICE) -> Backlog:
        async with self._sessions() as db:
            rows = (
                await db.scalars(
                    select(ConversationEvent)
                    .where(
                        ConversationEvent.conversation_id == conversation_id,
                        ConversationEvent.event_seq > after.event_seq,
                    )
                    .order_by(ConversationEvent.event_seq)
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
            return await stream_head(db, conversation_id)


async def stream_head(db: AsyncSession, conversation_id: UUID) -> StreamPosition:
    """Where this conversation's stream has got to, inside a caller's own transaction.

    Beside `ConversationStream.head` because a reader that also returns rows has to take the
    position **before** them, in the same read: a row written between the two is then replayed to
    the subscriber rather than reaching neither.
    """
    highest: int | None = await db.scalar(
        select(func.max(ConversationEvent.event_seq)).where(ConversationEvent.conversation_id == conversation_id)
    )
    return StreamPosition(event_seq=highest or START.event_seq)


def _streamed(row: ConversationEvent) -> StreamedEvent:
    return StreamedEvent(
        position=StreamPosition(event_seq=row.event_seq),
        session_id=row.session_id,
        turn_id=row.turn_id,
        item_id=row.item_id,
        created_at=row.created_at,
        body=session_events.body_of(row),
    )


class Cursor(Protocol):
    """Where one subscriber's position is kept.

    The only port here, because reading the stream is shared and keeping a place in it is not.
    """

    async def position(self) -> StreamPosition | None: ...

    async def keep(self, position: StreamPosition) -> None: ...


@dataclass(frozen=True, slots=True)
class ClientHeldCursor:
    """The position a subscriber carries itself: the read's own argument, and no server state.

    The SPA's form — `?after=N` on the request, with the answer's own position going back in the
    response — so several tabs can watch one conversation at different points and the console
    stores nothing about any of them.

    Never `Unstarted`: a request that names no position names `START`, which is a position.
    """

    after: StreamPosition

    async def position(self) -> StreamPosition:
        return self.after

    async def keep(self, position: StreamPosition) -> None:
        """Nothing is kept, and that is the implementation rather than an omission.

        The position went back with the events that produced it, and the client's next request is
        the only place it reappears.
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
