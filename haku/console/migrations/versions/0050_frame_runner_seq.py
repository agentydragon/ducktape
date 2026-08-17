"""Record the number the runner put on each frame, beside the one Postgres assigns.

`frame_seq` is an `Identity` — sparse and global. The runner mints a dense per-session number where
a frame goes on the wire (`ClaudeMessage.seq`, #4166); `runner_seq` is where the console keeps it,
so a reconnect can say "send me everything after N".

Nullable, and no backfill: no stored row carries the number. Sessions live at most
`session_ttl_seconds`, so the un-numbered population ages out on its own — which is the gate on
ever making this `NOT NULL`.

**The index is deliberately not unique.** The insert infers one conflict target and today's is
`frame_uid`, so a unique index here would turn a replayed frame with no agent-assigned identity — a
`control_response`, a `system` without a `task_id` — into a `UniqueViolation` that ends the session.
Uniqueness belongs with the release that moves the dedup onto this number.
The index serves the cursor read: `max(runner_seq)` for one session.

Revision ID: 0050
Revises: 0049
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0050"
down_revision: str | None = "0049"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("session_frames", sa.Column("runner_seq", sa.BigInteger(), nullable=True))
    op.create_index(
        "idx_session_frames_runner_seq",
        "session_frames",
        ["session_id", "runner_seq"],
        postgresql_where=sa.text("runner_seq IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("idx_session_frames_runner_seq", table_name="session_frames")
    op.drop_column("session_frames", "runner_seq")
