"""A reply the room has not been told yet is a row, not a closure.

`session_outbox` holds each reply a turn produced until the homeserver has accepted it. Written in
the same transaction as the assistant message it copies, so a turn that dies between producing text
and speaking it leaves the reply somewhere a later replica can find it.

**Additive, and safe for the length of a roll in both directions.** A replica on the previous image
never selects or inserts this table and keeps delivering through `matrix_pacer`'s in-process queue;
a replica on the new image writes rows and drains them under an advisory lock the old one does not
contend for. The overlap therefore has one writer per reply — whichever image ran the turn — so
nothing is delivered twice and nothing is stranded past the roll.

Revision ID: 0042
Revises: 0041
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0042"
down_revision: str | None = "0041"
branch_labels: str | None = None
depends_on: str | None = None

_TABLE = "session_outbox"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("outbox_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "session_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("sessions.session_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("room_id", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column(
            "message_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("session_messages.message_id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("agent_message_id", sa.Text(), nullable=True),
        sa.Column(
            "turn_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("session_turns.turn_id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.BigInteger(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
    )
    op.create_index(
        "idx_session_outbox_unsent", _TABLE, ["room_id", "created_at"], postgresql_where=sa.text("sent_at IS NULL")
    )
    op.create_index(
        "uq_session_outbox_message",
        _TABLE,
        ["message_id"],
        unique=True,
        postgresql_where=sa.text("message_id IS NOT NULL"),
    )
    op.create_index(
        "uq_session_outbox_turn", _TABLE, ["turn_id"], unique=True, postgresql_where=sa.text("turn_id IS NOT NULL")
    )
