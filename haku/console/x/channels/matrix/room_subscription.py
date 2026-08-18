"""The room's position in the conversation, and the notices it owes from it.

The Matrix half of <../../subscription.py>: the channel reads the record from a position of its own
instead of being handed events by whoever happens to be running the turn.

- **`RoomCursor`** is the durable position, in `matrix_room_cursor`. The room holds a federated copy
  that outlives every console process, so after a restart the channel has to know what it already
  put there. That durability is Matrix's own problem, kept in Matrix's own table below the channel
  boundary and beside the outbox; the conversation layer has no cursor table at all.
- **`RoomNotices`** is the subscriber. It wakes on `session_changed`, reads what the room has not
  been told, says it, and keeps the position it reached.

**Notices only, and not replies.** An answer is a `session_outbox` row the drain says into the room
with a transaction id, a retry budget and an ordering guarantee against other answers; a notice is
a rendering of a recorded fact and needs none of that. Moving the outbox is the reconciler's work
(<../../../plans/conversation_layers.md> § 5) and is deliberately not this.

**The position is kept after the notice, never before.** A crash in that window says the notice
again on the next pass — the same trade `delivery_log.retire` takes, and the right way round: a
room told twice is odd, a room never told is a message silently dropped. What is left unguarded is
the pacer's in-process queue, exactly as every other notice already is.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from haku.console.database_schema import MatrixRoomCursor
from haku.console.x.channels.matrix.conversation import Announce, MatrixConversationStore
from haku.console.x.channels.matrix.outbox import BoundRoom
from haku.console.x.session_events import TurnAbortedBody
from haku.console.x.session_notifications import SessionEventKind, SessionNotifications
from haku.console.x.subscription import ConversationStream, StreamedEvent, StreamPosition, Subscription, Unstarted

logger = logging.getLogger(__name__)

# Distinct from the sync loop's lock, the supervisor's, the outbox drain's and the OAuth sweep's.
_NOTICES_ADVISORY_LOCK = 0x4D58_4E54  # "MXNT"

# How this room renders a `turn_aborted` event. The words are the channel's own: what is recorded is
# that the turn was aborted, and every channel gets to say so differently.
ABORTED_BY_OPERATOR = "[aborted by operator]"

# How many events one pass renders. Small, because each one it does render costs the room a send.
NOTICE_BATCH = 50

# The backstop for what no notification reaches us with: `SessionNotifications.watch` cannot replay
# the ids that arrived while its listener was reconnecting, so a woken reader still has to look on
# its own.
POLL_INTERVAL = datetime.timedelta(seconds=10)

# How long a replica that lost the election waits before contending again.
LEADER_RETRY = datetime.timedelta(seconds=30)

# Backoff after a failed pass, so a database outage does not become a hot loop.
ERROR_BACKOFF = datetime.timedelta(seconds=10)


class RoomCursor:
    """Where this room's copy has been brought up to — `subscription.Cursor`, made durable.

    Absent means *never read*, not *at the start*, which is the distinction the reader needs: a
    room the console has been servicing since before this table existed already shows everything
    said in it, so replaying from zero would repeat the whole conversation into it.
    """

    def __init__(self, sessions: async_sessionmaker[AsyncSession], room_id: str) -> None:
        self._sessions = sessions
        self._room_id = room_id

    async def position(self) -> StreamPosition | None:
        async with self._sessions() as db:
            # Annotated because `AsyncSession.scalar` is typed `Any`, which `warn_return_any` refuses.
            reached: int | None = await db.scalar(
                select(MatrixRoomCursor.event_seq).where(MatrixRoomCursor.room_id == self._room_id)
            )
            return None if reached is None else StreamPosition(event_seq=reached)

    async def keep(self, position: StreamPosition) -> None:
        """Record that everything through *position* has reached the room.

        Upserted rather than read-then-written: the row is created by whichever replica holds the
        notices lock first, and an election that changes hands mid-conversation must not fail on the
        primary key.
        """
        async with self._sessions() as db, db.begin():
            await db.execute(
                insert(MatrixRoomCursor)
                .values(room_id=self._room_id, event_seq=position.event_seq)
                .on_conflict_do_update(index_elements=["room_id"], set_={"event_seq": position.event_seq})
            )


def notice(event: StreamedEvent) -> str | None:
    """What this room says about *event*, or nothing where it says nothing.

    A match on the event's body rather than on its kind, so a shape added to the stream lands here
    as a type error instead of being silently ignored. One arm today: everything else this room
    says about a recorded fact is either a reply (the outbox's) or is announced by ingress in the
    transaction that recorded it.
    """
    match event.body:
        case TurnAbortedBody():
            return ABORTED_BY_OPERATOR
        case _:
            return None


class RoomNotices:
    """Says what the record has recorded and the room has not been told, from the room's position."""

    def __init__(
        self,
        engine: AsyncEngine,
        sessions: async_sessionmaker[AsyncSession],
        stream: ConversationStream,
        conversations: MatrixConversationStore,
        notifications: SessionNotifications,
        announce: Announce,
        room: BoundRoom,
    ) -> None:
        self._engine = engine
        self._sessions = sessions
        self._stream = stream
        self._conversations = conversations
        self._notifications = notifications
        self._announce = announce
        self._room = room
        self._changed = asyncio.Event()

    async def reconcile_once(self) -> bool:
        """Say what the room is owed. True when the read stopped at its limit and there is more."""
        if (room_id := await self._room()) is None:
            return False
        if (conversation_id := await self._conversations.conversation_of_room(room_id)) is None:
            # A room this console holds no conversation for — never bound, or detached since — so
            # there is nothing recorded to be behind on.
            return False
        subscription = Subscription(self._stream, RoomCursor(self._sessions, room_id), conversation_id)
        read = await subscription.read(limit=NOTICE_BATCH)
        if isinstance(read, Unstarted):
            # Taken silently: the room already shows what was said in it for as long as it has been
            # bound, so rendering the stream from the start would repeat all of it.
            await subscription.keep(read.head)
            return False
        for event in read.events:
            if (body := notice(event)) is not None:
                await self._announce(body)
        await subscription.keep(read.position)
        return read.more

    def _wake(self, _session_id: UUID) -> None:
        """Note that some session's rows moved. Runs on the listener's reader task: no awaiting."""
        self._changed.set()

    async def _reconcile_as_leader(self) -> None:
        """Reconcile until cancelled. Only ever entered holding the advisory lock."""
        while True:
            try:
                # Cleared before the pass, so a change committed while it runs wakes the next one
                # instead of being cleared away after it was already missed.
                self._changed.clear()
                if not await self.reconcile_once():
                    with contextlib.suppress(TimeoutError):
                        async with asyncio.timeout(POLL_INTERVAL.total_seconds()):
                            await self._changed.wait()
            except Exception:
                logger.exception("Matrix: reconciling the room's notices failed")
                await asyncio.sleep(ERROR_BACKOFF.total_seconds())

    async def _run(self) -> None:
        while True:
            async with self._engine.connect() as leader:
                if not await leader.scalar(
                    text("SELECT pg_try_advisory_lock(:lock)"), {"lock": _NOTICES_ADVISORY_LOCK}
                ):
                    await asyncio.sleep(LEADER_RETRY.total_seconds())
                    continue
                logger.info("Matrix: this replica says the room's notices")
                try:
                    await self._reconcile_as_leader()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Matrix: the room's notice reader exited, retrying")
                    await asyncio.sleep(ERROR_BACKOFF.total_seconds())
                finally:
                    with contextlib.suppress(Exception):
                        await leader.scalar(text("SELECT pg_advisory_unlock(:lock)"), {"lock": _NOTICES_ADVISORY_LOCK})

    @asynccontextmanager
    async def run(self) -> AsyncIterator[None]:
        with self._notifications.watch(SessionEventKind.UPDATE, self._wake):
            task = asyncio.create_task(self._run(), name="matrix-room-notices")
            try:
                yield
            finally:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
