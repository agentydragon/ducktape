"""What a channel has put in its own copy: one live row per subject, and retiring frees the subject.

Against a real Postgres, because the promise is the partial unique index — a fake store asserting
"one live delivery per subject" would be asserting the fake.
"""

from __future__ import annotations

import datetime
from uuid import UUID, uuid4

import pytest
import pytest_bazel
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from haku.console.chat_models import ChatSurface
from haku.console.database_schema import ChatAttachment, Conversation
from haku.console.x.delivery_log import DeliveryLog


@pytest.fixture
def deliveries(migrated_sessions: async_sessionmaker[AsyncSession]) -> DeliveryLog:
    return DeliveryLog(migrated_sessions)


@pytest.fixture
async def attachment_id(migrated_sessions: async_sessionmaker[AsyncSession], operator_id: UUID) -> UUID:
    conversation_id, attachment_id = uuid4(), uuid4()
    now = datetime.datetime.now(datetime.UTC)
    async with migrated_sessions() as db, db.begin():
        db.add(Conversation(conversation_id=conversation_id, operator_id=operator_id, created_at=now))
        # Flushed before the row that points at it: the unit of work orders a flush from
        # `relationship()` dependencies and nothing else, so a bare `ForeignKey` leaves these in
        # mapper-name order — `chat_attachment` ahead of `conversation`.
        await db.flush()
        db.add(
            ChatAttachment(
                attachment_id=attachment_id,
                conversation_id=conversation_id,
                surface=ChatSurface.MATRIX,
                address="!room:example.org",
                attached_at=now,
                detached_at=None,
            )
        )
    return attachment_id


async def test_a_recorded_delivery_is_found_by_its_subject(deliveries: DeliveryLog, attachment_id: UUID) -> None:
    await deliveries.record(attachment_id, "status", "$line")

    showing = await deliveries.live(attachment_id, "status")
    assert showing is not None
    assert showing.sent_ref == "$line"


async def test_a_subject_nobody_has_shown_is_absent(deliveries: DeliveryLog, attachment_id: UUID) -> None:
    assert await deliveries.live(attachment_id, "status") is None


async def test_one_subject_cannot_be_showing_in_two_places(deliveries: DeliveryLog, attachment_id: UUID) -> None:
    """The index is the idempotence: without it a second pass over the same subject leaves two live
    rows and the reconciler has no answer to "which event is this"."""
    await deliveries.record(attachment_id, "status", "$line")

    with pytest.raises(IntegrityError):
        await deliveries.record(attachment_id, "status", "$second-line")


async def test_retiring_one_frees_its_subject(deliveries: DeliveryLog, attachment_id: UUID) -> None:
    await deliveries.record(attachment_id, "status", "$line")
    showing = await deliveries.live(attachment_id, "status")
    assert showing is not None

    await deliveries.retire(showing.delivery_id)
    await deliveries.record(attachment_id, "status", "$next-line")

    reshowing = await deliveries.live(attachment_id, "status")
    assert reshowing is not None
    assert reshowing.sent_ref == "$next-line"


async def test_a_detached_attachment_takes_its_deliveries_with_it(
    deliveries: DeliveryLog, migrated_sessions: async_sessionmaker[AsyncSession], attachment_id: UUID
) -> None:
    """A conversation the channel no longer holds a copy of has no copy to reconcile against, so
    what was in it is not state anything owes work on."""
    await deliveries.record(attachment_id, "status", "$line")

    async with migrated_sessions() as db, db.begin():
        attachment = await db.get(ChatAttachment, attachment_id)
        assert attachment is not None
        await db.delete(attachment)

    assert await deliveries.live(attachment_id, "status") is None


if __name__ == "__main__":
    pytest_bazel.main()
