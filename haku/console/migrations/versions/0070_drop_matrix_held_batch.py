"""Drop `matrix_held_batch`, which nothing maps any more. **Destructive.**

The third of the three steps the README's Perimeter / deploy section requires. `0043` created the
table so a batch handed to a session could stay unacknowledged until the turn answering it ended;
#4291 reversed that position — a prompt the session will not take is rejected rather than held, so
acceptance is the acknowledgement — and with the hold gone nothing writes or maps the table.

**Gated on the unmapping having converged, not on a release having elapsed.** An ORM-mapped table is
named in every `SELECT` SQLAlchemy emits for it whether or not any code reads the attribute, so a
replica still on the mapping image would fail the moment this runs, and `maxUnavailable: 0` lets a
stalled roll keep one serving indefinitely. Checked before this landed: both `haku-console` pods on
`devel-20260817104540-3d909fb`, one tag, and #4291 (`d1640f79df`) an ancestor of `3d909fb`.

The column types are spelled out in `downgrade` rather than imported from `0043`, for the reason
`0041` gives.

Revision ID: 0070
Revises: 0069
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0070"
down_revision: str | None = "0069"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.drop_table("matrix_held_batch")


def downgrade() -> None:
    """Puts the table back empty, which is what an image before #4291 expects to find.

    Nothing refills it: a held batch was live state naming a `/sync` token and the prompt row it was
    waiting on, and both are stale by the time a downgrade runs. An empty table is what the older
    code reads when no batch is held, which is the state it recovers into — it re-syncs from the
    watermark and the homeserver re-offers whatever was not acknowledged.
    """
    op.create_table(
        "matrix_held_batch",
        sa.Column("user_id", sa.Text(), primary_key=True),
        sa.Column("next_batch", sa.Text(), nullable=False),
        sa.Column(
            "message_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("session_messages.message_id", ondelete="CASCADE"),
            nullable=False,
        ),
    )
