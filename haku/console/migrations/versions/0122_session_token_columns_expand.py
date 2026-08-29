"""Expand the session-token columns: add the session-token names beside the bridge names.

C16a of the #4772 vocabulary collapse (naming_and_layout.md §3.6/§3.7): the stored
`bridge_token_fingerprint`/`bridge_connected_at` become `session_token_fingerprint`/
`runner_connected_at`. This is the **expand** release of a stored-column rename — the C4d recipe
(0114) — run while previous API replicas still serve, so both column pairs coexist: the new image
dual-writes both and keeps *reading* the bridge names, and no replica reads a column another has
renamed away.

The new columns are nullable because a still-serving pre-expand replica inserts and allocates
sessions without them during the roll. The read-switch release backfills those stragglers, moves
the CHECKs and the partial lease index onto the new names, and switches reads; later releases stop
writing the bridge names and then drop them. The conversation-droppable allowance (AGENTS.md)
would permit emptying the cascade instead of backfilling, but the backfill preserves the rows for
free and is the smaller change (as in 0114).

Revision ID: 0122
Revises: 0121
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0122"
down_revision: str | None = "0121"
branch_labels: str | None = None
depends_on: str | None = None

_UNIQUE = "uq_sessions_session_token_fingerprint"


def upgrade() -> None:
    op.add_column("sessions", sa.Column("session_token_fingerprint", sa.LargeBinary(), nullable=True))
    op.add_column("sessions", sa.Column("runner_connected_at", sa.DateTime(timezone=True), nullable=True))
    op.execute(
        "UPDATE sessions SET session_token_fingerprint = bridge_token_fingerprint, "
        "runner_connected_at = bridge_connected_at"
    )
    # Mirrors uq_sessions_bridge_token_fingerprint: NULLs (a pre-expand replica's roll-window
    # writes) pass, dual-written values keep the old constraint's uniqueness.
    op.create_unique_constraint(_UNIQUE, "sessions", ["session_token_fingerprint"])


def downgrade() -> None:
    op.drop_constraint(_UNIQUE, "sessions", type_="unique")
    op.drop_column("sessions", "runner_connected_at")
    op.drop_column("sessions", "session_token_fingerprint")
