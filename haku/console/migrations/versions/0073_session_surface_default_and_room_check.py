"""`sessions.surface` gets a server default, and `ck_sessions_matrix_room` goes.

Both stand in the way of unmapping `surface` and `room_id`, and each for its own reason:

- **`surface` is `NOT NULL` with no default.** SQLAlchemy names only mapped columns in an `INSERT`,
  so the release that unmaps it omits it and Postgres rejects the first session of that roll — the
  same hazard `0062` fixed for `session_frames.partial`, and the same fix.
- **`ck_sessions_matrix_room` couples the two columns**, `(surface = 'matrix') = (room_id IS NOT
  NULL)`. While it stands, unmapping either column alone writes a row the other half of the
  equivalence rejects, so it has to go before either does.

`'spa'` is spelled out rather than taken from `ChatSurface.SPA`, for the reason `0041` gives: a
migration is a point-in-time statement about the database, and reaching into code that moves would
make an already-applied migration change meaning.

The pairing rule itself stays enforced where it is decided — `SessionStore.create` takes a
`SpaSession | MatrixSession` variant and reads both columns off it, so the combinations the CHECK
rejected are unrepresentable at the one writer, and neither column is ever updated afterwards.

Revision ID: 0073
Revises: 0072
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0073"
down_revision: str | None = "0072"
branch_labels: str | None = None
depends_on: str | None = None

_MATRIX_ROOM = "ck_sessions_matrix_room"
_MATRIX_ROOM_CHECK = "(surface = 'matrix') = (room_id IS NOT NULL)"


def upgrade() -> None:
    op.alter_column(
        "sessions", "surface", existing_type=sa.Text(), existing_nullable=False, server_default=sa.text("'spa'")
    )
    op.drop_constraint(_MATRIX_ROOM, "sessions", type_="check")


def downgrade() -> None:
    op.create_check_constraint(_MATRIX_ROOM, "sessions", _MATRIX_ROOM_CHECK)
    op.alter_column("sessions", "surface", existing_type=sa.Text(), existing_nullable=False, server_default=None)
