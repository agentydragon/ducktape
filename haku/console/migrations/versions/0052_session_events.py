"""The neutral conversation events get rows.

`session_events` stores what the fold makes of a session's frames — messages, reasoning, harness
activity, and tool calls **with their answers**, which no row has held before: `session_messages`
keeps a call's arguments and the frames carrying its result are re-parsed on every read
(`x/session_views.rollout_calls`).

**Provenance is a union, and that is what makes the constraint expressible.** `provenance` is NOT
NULL and names the arm — `frame_range` or `authored` — with the frame columns present on exactly
the first. On `session_messages` the same question is two NULLs meaning two things and no `CHECK`
can separate them (#4143); here an event that crossed no wire says so, and one that did cannot be
written without saying where from.

**Additive, and safe for the length of a roll.** A new table with no other table referencing it, so
a replica on the previous image is untouched. It is also not backfilled — a row here is written in
the same transaction as the projection cursor that makes it exactly-once, and there is no cursor for
a session that predates one.

Revision ID: 0052
Revises: 0051
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0052"
down_revision: str | None = "0051"
branch_labels: str | None = None
depends_on: str | None = None

_TABLE = "session_events"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("event_seq", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column(
            "session_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("sessions.session_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "turn_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("session_turns.turn_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("provenance", sa.Text(), nullable=False),
        sa.Column("source_first_frame_seq", sa.BigInteger(), nullable=True),
        sa.Column("source_last_frame_seq", sa.BigInteger(), nullable=True),
        sa.Column("call_id", sa.Text(), nullable=True),
        sa.Column("body", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "kind IN ('message_completed','reasoning','tool_call_started','tool_call_completed',"
            "'activity_started','activity_completed')",
            name="ck_session_events_kind",
        ),
        sa.CheckConstraint("provenance IN ('frame_range','authored')", name="ck_session_events_provenance"),
        sa.CheckConstraint(
            "(provenance = 'frame_range') = (source_first_frame_seq IS NOT NULL) "
            "AND (source_first_frame_seq IS NULL) = (source_last_frame_seq IS NULL) "
            "AND (source_first_frame_seq IS NULL OR source_first_frame_seq <= source_last_frame_seq)",
            name="ck_session_events_provenance_frames",
        ),
        sa.CheckConstraint(
            "(call_id IS NOT NULL) = (kind IN ('tool_call_started','tool_call_completed'))",
            name="ck_session_events_call_id",
        ),
    )
    op.create_index("idx_session_events_session", _TABLE, ["session_id", "event_seq"])


def downgrade() -> None:
    op.drop_table(_TABLE)
