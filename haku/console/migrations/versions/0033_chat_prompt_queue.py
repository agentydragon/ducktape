"""The pending prompt is a queue row, not a status on a transcript row.

Revision ID: 0033
Revises: 0032
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0033"
down_revision: str | None = "0032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Additive: one new table, nothing altered. A replica on the previous image keeps enqueuing by
    # writing a `pending` message row and keeps dequeuing by scanning for one, which this release
    # still does alongside the queue — see `ClaudeChatStore.next_prompt`.
    #
    # Nothing is backfilled. A prompt that is pending *right now* is claimed by whichever replica
    # is serving that session, and inventing a queue row for it would either double-answer it or
    # block the session behind a row nobody claims.
    op.create_table(
        "claude_chat_prompts",
        sa.Column("prompt_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "session_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("claude_chat_sessions.session_id", ondelete="CASCADE"),
            nullable=False,
        ),
        # The transcript row minted with this prompt, and where its text lives: the queue holds no
        # copy of what was asked, so the two cannot come to disagree about it.
        sa.Column(
            "message_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("claude_chat_messages.message_id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("queued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Oldest first, which is the order `next_prompt` claims in.
    op.create_index("idx_claude_chat_prompts_session", "claude_chat_prompts", ["session_id", "queued_at"])
    # "One prompt in flight per session", which used to be a scan of the transcript for a `pending`
    # row plus the rule that only one exists. Here it is a property of the schema, so two replicas
    # racing on the same session cannot both conclude they may accept.
    op.create_index(
        "uq_claude_chat_prompts_unclaimed",
        "claude_chat_prompts",
        ["session_id"],
        unique=True,
        postgresql_where=sa.text("claimed_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_table("claude_chat_prompts")
