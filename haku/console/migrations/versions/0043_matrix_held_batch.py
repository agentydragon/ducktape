"""A batch handed to a session is not a batch acknowledged (R2.5).

`matrix_held_batch` holds the `/sync` token of a batch that has reached a session, until the turn
answering it has ended. The watermark in `matrix_sync_state` stays where a crash would have to
resume from until then, which is what stops a session dying between the enqueue and the turn from
acknowledging a message nobody will ever answer.

**Additive, and safe for the length of a roll.** A replica on the previous image never selects or
inserts this table and keeps acknowledging at enqueue exactly as before, and only one replica
syncs at a time (the `MXSY` advisory lock), so the two behaviours never interleave within a batch.
The one skew is a row the new image wrote that an *old* leader then ran past: when a new leader
takes over it publishes the older token it was holding, so the batches the old replica
acknowledged in between are delivered a second time. Re-delivery is the failure R2.5 asks the
system to be safe against ("re-running a batch must be safe"), where a skip is the one it
forbids — and it needs leadership to move new → old → new, which a `maxUnavailable: 0` roll does
not do.

Revision ID: 0043
Revises: 0042
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0043"
down_revision: str | None = "0042"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
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
