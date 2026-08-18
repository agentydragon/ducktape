"""Drop `sessions.room_id`, which the ORM no longer maps. **Destructive.**

The last of the three steps the README's Perimeter / deploy section requires for a mapped column:
`0064` gave a session its room through `conversation_id` and that conversation's live
`chat_attachment`, `0075` freed the column from `ck_sessions_matrix_room`, #4341 stopped mapping it,
and this drops it.

Gated on that unmapping having converged, not on a release having elapsed: an ORM-mapped column is
named in every `SELECT` SQLAlchemy emits for `Session` whether or not any code reads the attribute,
so a replica still on the mapping image would fail on every session read the moment this runs — and
`maxUnavailable: 0` lets a stalled roll keep such a replica serving. Read both pods' image tags
before applying this, as `0068` and `0069` did.

Nothing goes with the column: `ck_sessions_matrix_room` was the only constraint that named it, no
index ever did, and `0075` dropped the constraint.

Revision ID: 0079
Revises: 0078
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0079"
down_revision: str | None = "0078"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.drop_column("sessions", "room_id")


def downgrade() -> None:
    """Puts the column back empty, which is what an earlier image expects to find.

    Nothing backfills it. An image that selects this column names it because it is mapped, not
    because anything reads the value — a session's room is the `address` of the live
    `chat_attachment` on its conversation.
    """
    op.add_column("sessions", sa.Column("room_id", sa.Text(), nullable=True))
