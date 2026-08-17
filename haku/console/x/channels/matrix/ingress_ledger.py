"""Which inbound events the record already carries, and which of them it still owes an answer for.

Ingress acknowledges a batch by advancing the watermark, and that commit is not the one that wrote
the prompt. A crash in between re-delivers messages the session already holds, so the loop needs a
key of its own to recognise them by — the Matrix `event_id`, which is stable across a re-delivery
where a stream position is not.

**Suppression is not acknowledgement**, which is why this is a ledger and not a set of event ids
the loop has seen. A row exists only because a prompt exists, written in that prompt's transaction;
so an event this suppresses is one the record demonstrably holds. And a prompt whose session died
before claiming it is still owed an answer — `unanswered` is how the loop finds it and offers it
again, which a bare "skip what we have seen" would turn into a lost message.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from haku.console.chat_models import ENDED_SESSION_STATUSES
from haku.console.database_schema import MatrixIngressEvent, Session, SessionMessage, SessionPrompt
from haku.console.x.session_store import PromptRecords


@dataclass(frozen=True)
class Unanswered:
    """A prompt ingress accepted that no session is going to run.

    `text` is the prompt as it was rendered, so offering it again asks the same question rather
    than a reconstruction of it; `event_ids` are the events it carries, which move to the prompt
    that replaces it.
    """

    text: str
    event_ids: tuple[str, ...]


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
        """Record *event_ids* against the prompt being written, in that prompt's own transaction.

        Upserted rather than inserted, because re-offering an unanswered prompt writes the same
        events against the newer one: a row names whichever prompt is answering for the event now,
        and the prompt it named before is transcript.
        """

        async def record(db: AsyncSession, message_id: UUID) -> None:
            await db.execute(
                insert(MatrixIngressEvent)
                .values([{"event_id": event_id, "message_id": message_id} for event_id in event_ids])
                .on_conflict_do_update(index_elements=["event_id"], set_={"message_id": message_id})
            )

        return record

    async def unanswered(self) -> Unanswered | None:
        """The oldest accepted prompt whose session ended without ever claiming it.

        A session that is merely between turns is excluded: its queued prompt is work in hand, and
        the harness will take it. What this finds is the prompt nothing can reach any more — the
        window a killed sandbox opens, where the batch was acknowledged to the homeserver and the
        session holding it died before the turn that would have answered it.

        **Unscoped, because the bot serves one room.** Whatever gives it a second one has to give
        this a room to ask about, or a message outstanding in one room is offered into another.
        """
        stranded = (
            select(SessionMessage.message_id, SessionMessage.content)
            .join(SessionPrompt, SessionPrompt.message_id == SessionMessage.message_id)
            .join(Session, Session.session_id == SessionMessage.session_id)
            .where(
                SessionPrompt.claimed_at.is_(None),
                Session.status.in_(ENDED_SESSION_STATUSES),
                SessionMessage.message_id.in_(select(MatrixIngressEvent.message_id)),
            )
            .order_by(SessionMessage.created_at)
            .limit(1)
        )
        async with self._sessions() as db:
            if (found := (await db.execute(stranded)).first()) is None:
                return None
            message_id, text = found
            events = await db.scalars(
                select(MatrixIngressEvent.event_id)
                .where(MatrixIngressEvent.message_id == message_id)
                .order_by(MatrixIngressEvent.event_id)
            )
            return Unanswered(text=text, event_ids=tuple(events.all()))
