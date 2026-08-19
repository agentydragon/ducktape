"""Which homeserver event this channel is currently editing for a revisable subject.

**The Matrix channel's own, not a shared surface.** What this replaces was channel-generic and kept
a row per delivered message — a flushed-up-to position materialised one row at a time, which
`channel_cursor` holds properly. What is left is the part only a channel that can *edit* what it
sent has any use for: a status line it revises and later retires.

Both strings pass through untouched. `subject` is what the channel decided to show and `event_id`
is where it put it; nothing above the channel boundary interprets either.

`sent` returns a row rather than writing it, so a caller that wants the revision and its own
bookkeeping to commit together can add it to that transaction.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from haku.console.database_schema import MatrixRevision


def sent(*, attachment_id: UUID, subject: str, event_id: str, now: datetime.datetime) -> MatrixRevision:
    return MatrixRevision(
        revision_id=uuid4(),
        attachment_id=attachment_id,
        subject=subject,
        event_id=event_id,
        sent_at=now,
        retired_at=None,
    )


@dataclass(frozen=True)
class Revision:
    """One thing the channel is currently showing and can still edit."""

    revision_id: UUID
    event_id: str


class RevisionLog:
    """The revisable subjects an attachment currently shows, and the retirement of one."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]):
        self._sessions = sessions

    async def live(self, attachment_id: UUID, subject: str) -> Revision | None:
        async with self._sessions() as db:
            row: MatrixRevision | None = await db.scalar(
                select(MatrixRevision).where(
                    MatrixRevision.attachment_id == attachment_id,
                    MatrixRevision.subject == subject,
                    MatrixRevision.retired_at.is_(None),
                )
            )
            return None if row is None else Revision(revision_id=row.revision_id, event_id=row.event_id)

    async def record(self, attachment_id: UUID, subject: str, event_id: str) -> None:
        async with self._sessions() as db, db.begin():
            db.add(
                sent(
                    attachment_id=attachment_id,
                    subject=subject,
                    event_id=event_id,
                    now=datetime.datetime.now(datetime.UTC),
                )
            )

    async def retire(self, revision_id: UUID) -> None:
        """Record that the channel has taken this one back, freeing its subject.

        **Call it after the withdrawal has returned, never before.** A crash in that window then
        leaves a live row naming an event that is already gone, which the next pass repairs;
        retiring first would leave the copy showing something nothing remembers sending.
        """
        async with self._sessions() as db, db.begin():
            if (row := await db.get(MatrixRevision, revision_id)) is not None and row.retired_at is None:
                row.retired_at = datetime.datetime.now(datetime.UTC)
