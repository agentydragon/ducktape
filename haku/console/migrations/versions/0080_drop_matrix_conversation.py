"""`matrix_conversation` goes: the room binding lives on `chat_attachment`.

The table recorded one room per bot user, with a `session_id` pointer that had to be re-aimed every
time a session was replaced. `chat_attachment` holds the same binding keyed on the conversation, so
which session serves a room is derived rather than stored, and a second room no longer displaces the
first.

**Safe only because the release that unmapped the table has converged.** The console rolls with
`maxUnavailable: 0`, so a replica on the previous image serves against this schema for the length of
the roll — and an image that maps the table names it in the `SELECT` it emits, which a dropped table
answers with an error rather than with a stale value.

The reverse direction recreates the table empty. Nothing has written a row since the release that
stopped writing `session_id`, and the readers that would have wanted its contents are gone, so there
is nothing to restore into it.

Revision ID: 0080
Revises: 0079
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PGUUID

revision: str = "0080"
down_revision: str | None = "0079"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.drop_table("matrix_conversation")


def downgrade() -> None:
    op.create_table(
        "matrix_conversation",
        sa.Column("user_id", sa.Text(), primary_key=True),
        sa.Column("room_id", sa.Text(), nullable=False),
        sa.Column(
            "session_id", PGUUID(as_uuid=True), sa.ForeignKey("sessions.session_id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column("joined_at", sa.DateTime(timezone=True), nullable=False),
    )
