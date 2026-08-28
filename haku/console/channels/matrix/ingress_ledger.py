"""Which inbound events the record already carries.

Ingress acknowledges a batch by advancing the watermark, and that commit is not the one that wrote
the prompt. A crash in between re-delivers messages the record already holds, so the loop needs a
key of its own to recognise them by — the Matrix `event_id`, which is stable across a re-delivery
where a stream position is not.

**Suppression is not acknowledgement**, which is why this is a ledger and not a set of event ids
the loop has seen. A row exists only because a prompt exists, written in that prompt's transaction;
so an event this suppresses is one the record demonstrably holds, and a prompt in the record is a
prompt some session will answer — the queue is the conversation's, so the death of the session that
accepted it strands nothing.
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from haku.console.database_schema import MatrixIngressEvent
from haku.console.session.store import PromptRecords


class IngressLedger:
    """The `matrix_ingress_event` table, read and written by the Matrix channel alone."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]):
        self._sessions = sessions

    async def carried(self, event_ids: Sequence[str]) -> frozenset[str]:
        """Of *event_ids*, those a prompt in the record already carries."""
        async with self._sessions() as db:
            found = await db.scalars(
                select(MatrixIngressEvent.event_id).where(MatrixIngressEvent.event_id.in_(event_ids))
            )
            return frozenset(found.all())

    def carrying(self, event_ids: Sequence[str]) -> PromptRecords:
        """Record *event_ids* against the inbox prompt being written, in that prompt's transaction.

        Upserted rather than inserted: two passes can race on one event, and the row is a pointer to
        whichever prompt answers for it rather than a claim about which was first. The pointer is the
        `submitted_prompt` id (#4667) — a prompt is a durable inbox command before it is any item.
        """

        async def record(db: AsyncSession, prompt_id: UUID) -> None:
            await db.execute(
                insert(MatrixIngressEvent)
                .values([{"event_id": event_id, "prompt_id": prompt_id} for event_id in event_ids])
                .on_conflict_do_update(index_elements=["event_id"], set_={"prompt_id": prompt_id})
            )

        return record
