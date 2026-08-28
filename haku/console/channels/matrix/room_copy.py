"""What the room durably shows of the console's own projected sends — the correspondence store.

One row per Haku-authored event whose tag names a durable conversation event
(`client.ProjectedEvent`), written by the sync loop as the events echo back through `/sync` and
read by the room's reconciler before it sends. The deterministic Matrix transaction id protects
only the window between a successful send and its echo becoming visible; past the echo this table
is what says the room already shows a source, however long ago the send happened.

**Fed by observation, never by the send path.** A row exists because `/sync` showed the event, so
the table cannot claim a send that never reached the room — the failure the transaction id cannot
distinguish is exactly the one this reader settles.

**A redaction keeps its row.** The room can no longer be asked what it used to show, but this store
was watching: a redacted copy still answers "this source reached the room once", so a cursor replay
does not re-post what the operator unsaid. What redaction removes is the row's standing as a live
copy — a redacted event is never a duplicate to repair, and repairing one would have been the room
fighting its own operator.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import groupby
from uuid import UUID

from sqlalchemy import exists, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from haku.console.channels.matrix.client import ProjectedEvent, Redaction
from haku.console.database_schema import ChannelAttachmentRow, MatrixRoomCopy

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RedundantCopy:
    """A live original past the first for one source — a duplicate its room is owed a redaction for.

    The attachment rides along because the duplicate need not be an event of the batch that
    revealed it, so its room is found through the copy's own attachment rather than assumed.
    """

    attachment_id: UUID
    event_id: str


class RoomCopy:
    """The `matrix_room_copy` table, read and written by the Matrix channel alone."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]):
        self._sessions = sessions

    async def record(
        self, projected: Sequence[ProjectedEvent], redactions: Sequence[Redaction]
    ) -> tuple[RedundantCopy, ...]:
        """Fold one pass's own-sender observations in; return the copies now redundant.

        Idempotent, so the caller may record before acknowledging the batch and a crash between
        the two re-records rather than forgets — which is what makes "the watermark is past the
        echo" imply "the correspondence is durable".

        The returned copies are live original posts that share a source with an earlier one:
        the duplicate a send past the transaction-cache window can leave behind, which the caller
        owes the room a redaction for. An edit (`m.replace`) revises an event the room already
        shows and is never one of them.
        """
        async with self._sessions() as db, db.begin():
            kept = await self._attached(db, projected) if projected else []
            if kept:
                # DO NOTHING rather than update: the first observation of an event is as good as
                # any later one, and a re-delivered event must not resurrect a recorded redaction.
                await db.execute(
                    insert(MatrixRoomCopy)
                    .values(
                        [
                            {
                                "event_id": event.event_id,
                                "attachment_id": event.source.attachment_id,
                                "source_event_seq": event.source.event_seq,
                                "replaces_event_id": event.replaces_event_id,
                                "origin_server_ts": event.origin_server_ts,
                                "redacted": False,
                            }
                            for event in kept
                        ]
                    )
                    .on_conflict_do_nothing(index_elements=["event_id"])
                )
            if redactions:
                await db.execute(
                    update(MatrixRoomCopy)
                    .where(MatrixRoomCopy.event_id.in_({redaction.redacts_event_id for redaction in redactions}))
                    .values(redacted=True)
                )
            return await self._redundant(db, {(event.source.attachment_id, event.source.event_seq) for event in kept})

    async def shows(self, attachment_id: UUID, source_event_seq: int) -> bool:
        """Whether this attachment's room has ever shown the given conversation event.

        Redacted copies count: the correspondence question is "did this projection reach the
        room", and re-sending what the operator redacted would answer a different one.
        """
        async with self._sessions() as db:
            shown: bool | None = await db.scalar(
                select(
                    exists().where(
                        MatrixRoomCopy.attachment_id == attachment_id,
                        MatrixRoomCopy.source_event_seq == source_event_seq,
                    )
                )
            )
            return bool(shown)

    async def _attached(self, db: AsyncSession, projected: Sequence[ProjectedEvent]) -> list[ProjectedEvent]:
        """The events whose tagged attachment still exists.

        A room event is permanent while an attachment row is not — conversation data may be
        dropped wholesale — so an event tagged with a dropped attachment must degrade to a logged
        skip rather than wedge the sync loop on a foreign key it can never satisfy.
        """
        tagged = {event.source.attachment_id for event in projected}
        known = set(
            (
                await db.scalars(
                    select(ChannelAttachmentRow.attachment_id).where(ChannelAttachmentRow.attachment_id.in_(tagged))
                )
            ).all()
        )
        for event in projected:
            if event.source.attachment_id not in known:
                logger.warning(
                    "Matrix: %s is tagged with attachment %s, which no longer exists; not recording it",
                    event.event_id,
                    event.source.attachment_id,
                )
        return [event for event in projected if event.source.attachment_id in known]

    async def _redundant(self, db: AsyncSession, touched: set[tuple[UUID, int]]) -> tuple[RedundantCopy, ...]:
        """Among the sources this pass touched, every live original past the first."""
        if not touched:
            return ()
        copies = (
            await db.execute(
                select(MatrixRoomCopy.attachment_id, MatrixRoomCopy.source_event_seq, MatrixRoomCopy.event_id)
                .where(
                    tuple_(MatrixRoomCopy.attachment_id, MatrixRoomCopy.source_event_seq).in_(touched),
                    MatrixRoomCopy.redacted.is_(False),
                    MatrixRoomCopy.replaces_event_id.is_(None),
                )
                # The earliest copy is the one to keep: it is the one the record's send produced,
                # and every later one is the replay that should have been refused.
                .order_by(
                    MatrixRoomCopy.attachment_id,
                    MatrixRoomCopy.source_event_seq,
                    MatrixRoomCopy.origin_server_ts,
                    MatrixRoomCopy.event_id,
                )
            )
        ).all()
        return tuple(
            RedundantCopy(attachment_id=copy.attachment_id, event_id=copy.event_id)
            for _, group in groupby(copies, key=lambda copy: (copy.attachment_id, copy.source_event_seq))
            for copy in list(group)[1:]
        )
