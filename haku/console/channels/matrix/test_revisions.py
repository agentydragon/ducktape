"""What a channel has put in its own copy: one live row per subject, and retiring frees the subject.

Against a real Postgres, because the promise is the partial unique index — a fake store asserting
"one live revision per subject" would be asserting the fake.
"""

from __future__ import annotations

import datetime
from uuid import UUID, uuid4

import pytest
import pytest_bazel
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from haku.console.channels.matrix.revisions import RevisionLog
from haku.console.channels.surface import ChannelSurface
from haku.console.database_schema import ChannelAttachmentRow, Conversation
from haku.console.harnesses.kind import HarnessKind


@pytest.fixture
def revisions(migrated_sessions: async_sessionmaker[AsyncSession]) -> RevisionLog:
    return RevisionLog(migrated_sessions)


@pytest.fixture
async def attachment_id(migrated_sessions: async_sessionmaker[AsyncSession], operator_id: UUID) -> UUID:
    conversation_id, attachment_id = uuid4(), uuid4()
    now = datetime.datetime.now(datetime.UTC)
    async with migrated_sessions() as db, db.begin():
        db.add(
            Conversation(
                conversation_id=conversation_id,
                operator_id=operator_id,
                harness_kind=HarnessKind.CLAUDE_CODE,
                created_at=now,
            )
        )
        # Flushed before the row that points at it: the unit of work orders a flush from
        # `relationship()` dependencies and nothing else, so a bare `ForeignKey` leaves these in
        # mapper-name order — `channel_attachment` ahead of `conversation`.
        await db.flush()
        db.add(
            ChannelAttachmentRow(
                attachment_id=attachment_id,
                conversation_id=conversation_id,
                surface=ChannelSurface.MATRIX,
                address="!room:example.org",
                attached_at=now,
                detached_at=None,
            )
        )
    return attachment_id


async def test_a_recorded_revision_is_found_by_its_subject(revisions: RevisionLog, attachment_id: UUID) -> None:
    await revisions.record(attachment_id, "status", "$line")

    showing = await revisions.live(attachment_id, "status")
    assert showing is not None
    assert showing.event_id == "$line"


async def test_a_subject_nobody_has_shown_is_absent(revisions: RevisionLog, attachment_id: UUID) -> None:
    assert await revisions.live(attachment_id, "status") is None


async def test_one_subject_cannot_be_showing_in_two_places(revisions: RevisionLog, attachment_id: UUID) -> None:
    """The index is the idempotence: without it a second pass over the same subject leaves two live
    rows and the reconciler has no answer to "which event is this"."""
    await revisions.record(attachment_id, "status", "$line")

    with pytest.raises(IntegrityError):
        await revisions.record(attachment_id, "status", "$second-line")


async def test_retiring_one_frees_its_subject(revisions: RevisionLog, attachment_id: UUID) -> None:
    await revisions.record(attachment_id, "status", "$line")
    showing = await revisions.live(attachment_id, "status")
    assert showing is not None

    await revisions.retire(showing.revision_id)
    await revisions.record(attachment_id, "status", "$next-line")

    reshowing = await revisions.live(attachment_id, "status")
    assert reshowing is not None
    assert reshowing.event_id == "$next-line"


async def test_a_detached_attachment_takes_its_revisions_with_it(
    revisions: RevisionLog, migrated_sessions: async_sessionmaker[AsyncSession], attachment_id: UUID
) -> None:
    """A conversation the channel no longer holds a copy of has no copy to reconcile against, so
    what was in it is not state anything owes work on."""
    await revisions.record(attachment_id, "status", "$line")

    async with migrated_sessions() as db, db.begin():
        attachment = await db.get(ChannelAttachmentRow, attachment_id)
        assert attachment is not None
        await db.delete(attachment)

    assert await revisions.live(attachment_id, "status") is None


if __name__ == "__main__":
    pytest_bazel.main()
