"""A turn's state lives on the turn row.

What `_run_turn` held in locals — the assistant message it is streaming into, whether it has
completed one, whether it has put a reply in the room's outbox — is now three columns, written in
the same transaction as the effect each describes. A replacement replica reads them instead of
reconstructing them from the frame log.

**Additive, and safe for the length of a roll.** A replica on the previous image never selects or
writes these columns, and their defaults are exactly the state it leaves behind. The backfill covers
the turns that are *open when this runs*, which is the population that would otherwise be adopted
onto empty state; a turn opened by an old replica afterwards and adopted by a new one resumes with
the defaults, costing at most the turn's last word queued a second time.

Revision ID: 0044
Revises: 0043
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0044"
down_revision: str | None = "0043"
branch_labels: str | None = None
depends_on: str | None = None

_TABLE = "session_turns"


def upgrade() -> None:
    op.add_column(
        _TABLE,
        sa.Column(
            "assistant_message_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("session_messages.message_id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(_TABLE, sa.Column("said_anything", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column(_TABLE, sa.Column("queued_reply", sa.Boolean(), nullable=False, server_default=sa.false()))

    # The same three questions the reconstruction helpers answered, asked once for the turns that
    # are open right now. `said_anything` reads the completed `assistant` frames of the turn's
    # range, excluding the console's own `partial` reconstruction of an answer still streaming;
    # `queued_reply` reads the outbox, because that is what the column means, and a session's
    # rows since the turn opened are this turn's — only one turn per session is ever open.
    op.execute(
        sa.text("""
        UPDATE session_turns AS t
           SET assistant_message_id = (
                   SELECT m.message_id
                     FROM session_messages AS m
                    WHERE m.session_id = t.session_id
                      AND m.role = 'assistant'
                      AND m.status = 'streaming'
                    ORDER BY m.created_at DESC
                    LIMIT 1),
               said_anything = EXISTS (
                   SELECT 1
                     FROM session_frames AS f
                    WHERE f.session_id = t.session_id
                      AND f.frame_seq >= t.first_frame_seq
                      AND f.direction = 'from_agent'
                      AND f.kind = 'assistant'
                      AND NOT f.partial),
               queued_reply = EXISTS (
                   SELECT 1
                     FROM session_outbox AS o
                    WHERE o.session_id = t.session_id
                      AND o.created_at >= t.started_at)
         WHERE t.ended_at IS NULL
        """)
    )
