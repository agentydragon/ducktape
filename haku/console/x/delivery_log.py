"""What a channel has put in its own copy, over `chat_delivery`.

Reads and writes the correspondence a copy-holding channel keeps: a subject it decided to show, and
where it put it. Both strings pass through untouched — this module never parses either, which is
what lets it be shared by every such channel without learning any of their vocabularies.

`sent` returns a row rather than writing it, so a channel that wants the delivery and its own
bookkeeping to commit together can add it to that transaction — the same shape
`session_events.authored` takes, and for the same reason.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from haku.console.database_schema import ChatDelivery


def sent(*, attachment_id: UUID, subject: str, sent_ref: str, now: datetime.datetime) -> ChatDelivery:
    return ChatDelivery(
        delivery_id=uuid4(),
        attachment_id=attachment_id,
        subject=subject,
        sent_ref=sent_ref,
        sent_at=now,
        retired_at=None,
    )


@dataclass(frozen=True)
class Delivery:
    """One thing a channel is currently showing, as the channel needs to see it."""

    delivery_id: UUID
    sent_ref: str


class DeliveryLog:
    """The deliveries an attachment currently holds, and the retirement of one."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]):
        self._sessions = sessions

    async def live(self, attachment_id: UUID, subject: str) -> Delivery | None:
        async with self._sessions() as db:
            row: ChatDelivery | None = await db.scalar(
                select(ChatDelivery).where(
                    ChatDelivery.attachment_id == attachment_id,
                    ChatDelivery.subject == subject,
                    ChatDelivery.retired_at.is_(None),
                )
            )
            return None if row is None else Delivery(delivery_id=row.delivery_id, sent_ref=row.sent_ref)

    async def record(self, attachment_id: UUID, subject: str, sent_ref: str) -> None:
        async with self._sessions() as db, db.begin():
            db.add(
                sent(
                    attachment_id=attachment_id,
                    subject=subject,
                    sent_ref=sent_ref,
                    now=datetime.datetime.now(datetime.UTC),
                )
            )

    async def retire(self, delivery_id: UUID) -> None:
        """Record that the channel has taken this one back, freeing its subject.

        **Call it after the withdrawal has returned, never before.** A crash in that window then
        leaves a live row naming an event that is already gone, which the next pass repairs;
        retiring first would leave the copy showing something nothing remembers sending.
        """
        async with self._sessions() as db, db.begin():
            if (row := await db.get(ChatDelivery, delivery_id)) is not None and row.retired_at is None:
                row.retired_at = datetime.datetime.now(datetime.UTC)
