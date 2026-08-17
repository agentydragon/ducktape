"""Where the Matrix room has been brought up to in the conversation it holds a copy of.

The room's position in <../../x/subscription.py>'s stream, kept in the channel's own table rather
than in a shared one: a browser tab's position vanishes with the tab and is a request parameter, so
only a channel holding a durable copy has anything to persist.

**Additive, and safe for the length of a roll.** A new table nothing else references: the previous
image neither writes it nor joins through it, and an absent row is the state the reader seeds
from.

Revision ID: 0071
Revises: 0070
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0071"
down_revision: str | None = "0070"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "matrix_room_cursor",
        sa.Column("room_id", sa.Text(), primary_key=True),
        sa.Column("event_seq", sa.BigInteger(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("matrix_room_cursor")
