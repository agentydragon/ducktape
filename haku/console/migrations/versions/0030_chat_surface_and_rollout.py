"""A chat session records the surface it served, and its agent's protocol frames are kept.

Revision ID: 0030
Revises: 0029
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0030"
down_revision: str | None = "0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Both halves are additive, so this is safe for the length of a roll: a replica on the
    # previous image keeps inserting sessions without the new columns and records no frames at
    # all, and neither is a schema disagreement.
    _add_session_surface()
    _create_frames()


def _add_session_surface() -> None:
    # Nullable and undefaulted, so rows written by the previous image read as "predates
    # attribution". Defaulting `surface` to 'spa' would label those rows confidently and wrongly.
    op.add_column("claude_chat_sessions", sa.Column("surface", sa.Text(), nullable=True))
    op.add_column("claude_chat_sessions", sa.Column("room_id", sa.Text(), nullable=True))
    op.create_check_constraint(
        "ck_claude_chat_sessions_surface", "claude_chat_sessions", "surface IS NULL OR surface IN ('spa','matrix')"
    )
    op.create_check_constraint(
        "ck_claude_chat_sessions_room_is_matrix", "claude_chat_sessions", "room_id IS NULL OR surface = 'matrix'"
    )
    op.create_check_constraint(
        "ck_claude_chat_sessions_matrix_has_room", "claude_chat_sessions", "surface <> 'matrix' OR room_id IS NOT NULL"
    )

    # The one session whose surface is still knowable. `matrix_conversation` holds a single
    # binding, so this recovers the live one and nothing before it: every Matrix session that
    # was already displaced is unattributable and stays null, which is the loss this migration
    # stops rather than repairs.
    op.execute(
        sa.text(
            "UPDATE claude_chat_sessions AS s"
            " SET surface = 'matrix', room_id = c.room_id"
            " FROM matrix_conversation AS c"
            " WHERE c.session_id = s.session_id"
        )
    )


def _create_frames() -> None:
    op.create_table(
        "claude_chat_frames",
        sa.Column("frame_seq", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column(
            "session_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("claude_chat_sessions.session_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("direction", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("partial", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("direction IN ('to_agent','from_agent')", name="ck_claude_chat_frames_direction"),
    )
    op.create_index("idx_claude_chat_frames_session", "claude_chat_frames", ["session_id", "frame_seq"])
    # At most one in-flight reconstruction per session; see the model's `partial` column.
    op.create_index(
        "uq_claude_chat_frames_partial",
        "claude_chat_frames",
        ["session_id"],
        unique=True,
        postgresql_where=sa.text("partial"),
    )


def downgrade() -> None:
    op.drop_index("uq_claude_chat_frames_partial", table_name="claude_chat_frames")
    op.drop_index("idx_claude_chat_frames_session", table_name="claude_chat_frames")
    op.drop_table("claude_chat_frames")
    op.drop_constraint("ck_claude_chat_sessions_matrix_has_room", "claude_chat_sessions", type_="check")
    op.drop_constraint("ck_claude_chat_sessions_room_is_matrix", "claude_chat_sessions", type_="check")
    op.drop_constraint("ck_claude_chat_sessions_surface", "claude_chat_sessions", type_="check")
    op.drop_column("claude_chat_sessions", "room_id")
    op.drop_column("claude_chat_sessions", "surface")
