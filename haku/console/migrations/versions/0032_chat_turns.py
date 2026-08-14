"""A turn exists: one exchange, bracketed as a range over the session's frame log.

Revision ID: 0032
Revises: 0031
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0032"
down_revision: str | None = "0031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Two new tables and nothing touched, so this is safe for the length of a roll: a replica on
    # the previous image opens no turns and reads none, which reads as a session with no turn
    # history rather than as a schema disagreement. Nothing is backfilled — a turn that ran
    # before this migration has no bracket to recover, and inventing one would put a guess in
    # the record.
    op.create_table(
        "claude_chat_turns",
        sa.Column("turn_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "session_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("claude_chat_sessions.session_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("first_frame_seq", sa.BigInteger(), nullable=False),
        sa.Column("last_frame_seq", sa.BigInteger(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("outcome", sa.Text(), nullable=True),
        sa.Column("cost_usd", sa.Numeric(12, 6), nullable=True),
        sa.Column("usage", postgresql.JSONB(), nullable=True),
        sa.Column("duration_ms", sa.BigInteger(), nullable=True),
        sa.CheckConstraint(
            "outcome IS NULL OR outcome IN ('answered','aborted','failed')", name="ck_claude_chat_turns_outcome"
        ),
        sa.CheckConstraint("(ended_at IS NULL) = (outcome IS NULL)", name="ck_claude_chat_turns_ended_has_outcome"),
    )
    op.create_index("idx_claude_chat_turns_session", "claude_chat_turns", ["session_id", "started_at"])
    # One open turn per session. This is what `responding` is derived from and what makes "an
    # abort names a turn" a lookup rather than a guess, so it is enforced here rather than left
    # to the turn loop: two replicas both believing they hold the session would otherwise each
    # open one, and the second would be invisible.
    op.create_index(
        "uq_claude_chat_turns_open",
        "claude_chat_turns",
        ["session_id"],
        unique=True,
        postgresql_where=sa.text("ended_at IS NULL"),
    )
    op.create_table(
        "claude_chat_turn_prompts",
        sa.Column(
            "turn_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("claude_chat_turns.turn_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "message_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("claude_chat_messages.message_id", ondelete="CASCADE"),
            primary_key=True,
        ),
    )


def downgrade() -> None:
    op.drop_table("claude_chat_turn_prompts")
    op.drop_table("claude_chat_turns")
