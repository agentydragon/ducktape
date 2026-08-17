"""Give a rollout frame the agent's own identity, so a replay can be recognised.

Additive, nullable, and deliberately not backfilled: a frame recorded before this has no
`frame_uid` and giving it one after the fact would claim that a history recorded without
deduplication was deduplicated. NULL says "predates this", which is what the partial index is
built for.

Revision ID: 0035
Revises: 0034
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0035"
down_revision: str | None = "0034"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("claude_chat_frames", sa.Column("frame_uid", sa.Text(), nullable=True))
    # Partial, because NULL is the common case and always will be: deltas have no identity, the
    # two console-authored kinds have none, and every row predating this has none. The `WHERE`
    # keeps the index the size of the rows it actually constrains.
    op.create_index(
        "uq_claude_chat_frames_uid",
        "claude_chat_frames",
        ["session_id", "frame_uid"],
        unique=True,
        postgresql_where=sa.text("frame_uid IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_claude_chat_frames_uid", table_name="claude_chat_frames")
    op.drop_column("claude_chat_frames", "frame_uid")
