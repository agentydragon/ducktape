"""Point projected messages back at the raw frames that produced them.

The message table is a deliberately lossy projection of the session frame log. Keeping the
inclusive source range makes that loss inspectable without copying the wire payload into every
message row.

Revision ID: 0045
Revises: 0044
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0045"
down_revision: str | None = "0044"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("session_messages", sa.Column("source_first_frame_seq", sa.BigInteger(), nullable=True))
    op.add_column("session_messages", sa.Column("source_last_frame_seq", sa.BigInteger(), nullable=True))
    op.create_check_constraint(
        "ck_session_messages_source_frames",
        "session_messages",
        "source_first_frame_seq IS NULL OR source_last_frame_seq IS NULL "
        "OR source_first_frame_seq <= source_last_frame_seq",
    )

    # Observed assistant rows already carry the agent's message id. Use it to recover the exact
    # assistant frame where possible; synthesized and older rows remain explicitly unpointed.
    op.execute(
        sa.text(
            """
            UPDATE session_messages AS message
            SET source_first_frame_seq = frames.first_frame_seq,
                source_last_frame_seq = frames.last_frame_seq
            FROM (
                SELECT message_id,
                       min(frame.frame_seq) AS first_frame_seq,
                       max(frame.frame_seq) AS last_frame_seq
                FROM session_messages AS message_row
                JOIN session_frames AS frame
                  ON frame.session_id = message_row.session_id
                 AND frame.kind = 'assistant'
                 AND frame.payload #>> '{message,id}' = message_row.agent_message_id
                WHERE message_row.agent_message_id IS NOT NULL
                GROUP BY message_id
            ) AS frames
            WHERE message.message_id = frames.message_id
            """
        )
    )


def downgrade() -> None:
    op.drop_constraint("ck_session_messages_source_frames", "session_messages", type_="check")
    op.drop_column("session_messages", "source_last_frame_seq")
    op.drop_column("session_messages", "source_first_frame_seq")
