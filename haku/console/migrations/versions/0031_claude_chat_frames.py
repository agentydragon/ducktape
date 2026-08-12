"""The agent's protocol frames are kept, so a past session's rollout can be read back.

Revision ID: 0031
Revises: 0030
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0031"
down_revision: str | None = "0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # A new table only, so a replica on the previous image is unaffected for the length of a
    # roll: it simply records nothing, and the rows it does not write are a gap in one
    # session's rollout rather than a schema disagreement.
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
