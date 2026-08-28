"""Key `session_events` to the conversation, and widen its kinds. **Permissive throughout.**

The neutral log is the conversation's record wearing a session's key. This gives it the conversation
as a column, frees `session_id` to be absent, and admits the five kinds that record what only a
stack frame holds today. Nothing writes any of it in this release — see below, because that is the
point rather than an omission.

**`conversation_id` arrives nullable and stays nullable here.** The previous image inserts rows
without naming it for the length of every roll, and no column default can express "this session's
conversation", so `SET NOT NULL` is a later migration. That one re-runs the backfill first: rows
written between this migration and the release where every writer names the column are NULL too, and
the `UPDATE` below only covers what exists when it runs.

**The backfill is total.** `session_events.session_id` is `NOT NULL` with an `ON DELETE CASCADE`
foreign key, so no row can outlive its session, and `sessions.conversation_id` has been `NOT NULL`
since `0072`. There is no orphan arm because there can be no orphan.

**`session_id DROP NOT NULL` is permissive.** The previous image always supplies one, so it produces
no NULL, and nothing reads a NULL until a writer makes one. `ck_session_events_frame_range_session`
is added in the same breath to keep the loss narrow: a row folded out of frames is a session's by
construction, so only the authored arm may omit it.

**The widened `kind` CHECK forbids nothing and is deliberately ahead of its writers.** A row of an
unknown kind fails on *read*: `TextBackedStrEnumUnionColumn.process_result_value` raises `KeyError`,
and `session/subscription.ConversationStream.read` — driven by the Matrix conversation subscriber under the `MXNT` election —
selects whole rows with no kind filter. `0065` could argue this was safe because the two readers
then filtered in SQL; that reader did not exist yet and the argument has since expired. So this
release teaches every replica to parse the new kinds and writes none of them, and the writers land
once it has converged.

Revision ID: 0082
Revises: 0081
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0082"
down_revision: str | None = "0081"
branch_labels: str | None = None
depends_on: str | None = None

_TABLE = "session_events"
_KIND = "ck_session_events_kind"
_FRAME_RANGE_SESSION = "ck_session_events_frame_range_session"
_CONVERSATION_INDEX = "idx_session_events_conversation"

# Spelled out rather than imported from the ORM, for the reason `0041` gives: a migration is a
# point-in-time statement about the database and must not change meaning when another file is edited.
_KINDS_BEFORE = (
    "'message_completed','reasoning','tool_call_started','tool_call_completed',"
    "'prompt_enqueued','prompt_rejected','unreadable_input','session_adopted',"
    "'lease_expired','turn_aborted'"
)
_KINDS_ADDED = "'session_provisioning','session_ended','setup_narration','turn_started','turn_ended'"


def upgrade() -> None:
    op.add_column(
        _TABLE,
        sa.Column(
            "conversation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("conversation.conversation_id", ondelete="CASCADE"),
            nullable=True,
        ),
    )
    op.execute(
        sa.text(
            "UPDATE session_events e SET conversation_id = s.conversation_id "
            "FROM sessions s WHERE s.session_id = e.session_id"
        )
    )
    op.create_index(_CONVERSATION_INDEX, _TABLE, ["conversation_id", "event_seq"])
    op.alter_column(_TABLE, "session_id", existing_type=postgresql.UUID(as_uuid=True), nullable=True)
    op.create_check_constraint(_FRAME_RANGE_SESSION, _TABLE, "provenance <> 'frame_range' OR session_id IS NOT NULL")
    op.drop_constraint(_KIND, _TABLE, type_="check")
    op.create_check_constraint(_KIND, _TABLE, f"kind IN ({_KINDS_BEFORE},{_KINDS_ADDED})")


def downgrade() -> None:
    """Narrowing `kind` deletes what it can no longer hold, as `0065` did for `turn_aborted`.

    `SET NOT NULL` on `session_id` needs no backfill for the same reason the upgrade needs no orphan
    arm: nothing in this release writes a row without one.
    """
    op.execute(sa.text(f"DELETE FROM {_TABLE} WHERE kind IN ({_KINDS_ADDED})"))
    op.drop_constraint(_KIND, _TABLE, type_="check")
    op.create_check_constraint(_KIND, _TABLE, f"kind IN ({_KINDS_BEFORE})")
    op.drop_constraint(_FRAME_RANGE_SESSION, _TABLE, type_="check")
    op.alter_column(_TABLE, "session_id", existing_type=postgresql.UUID(as_uuid=True), nullable=False)
    op.drop_index(_CONVERSATION_INDEX, table_name=_TABLE)
    # The foreign key goes with the column, as `0081` notes for a CHECK in the same position.
    op.drop_column(_TABLE, "conversation_id")
