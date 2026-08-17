"""`sessions.surface` becomes nullable, and `ck_sessions_matrix_room` goes.

Both stand in the way of unmapping `surface` and `room_id`, and each for its own reason:

- **`surface` is `NOT NULL` with no default.** SQLAlchemy names only mapped columns in an `INSERT`,
  so the release that unmaps it omits it and Postgres rejects the first session of that roll.
  Nullable rather than defaulted (operator, 2026-08-17): a default would record every Matrix room's
  sessions as `'spa'` for the release between the unmapping and the drop — a false statement about
  the very linkage the attachment replaces. Absent says what is true, that the session no longer
  states a surface.
- **`ck_sessions_matrix_room` couples the two columns**, `(surface = 'matrix') = (room_id IS NOT
  NULL)`. While it stands, unmapping either column alone writes a row the other half of the
  equivalence rejects, so it has to go before either does.

The pairing rule itself stays enforced where it is decided — `SessionStore.create` takes a
`SpaSession | MatrixSession` variant and reads both columns off it, so the combinations the CHECK
rejected are unrepresentable at the one writer, and neither column is ever updated afterwards.

Revision ID: 0075
Revises: 0074
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0075"
down_revision: str | None = "0074"
branch_labels: str | None = None
depends_on: str | None = None

_MATRIX_ROOM = "ck_sessions_matrix_room"
_MATRIX_ROOM_CHECK = "(surface = 'matrix') = (room_id IS NOT NULL)"


def upgrade() -> None:
    op.alter_column("sessions", "surface", existing_type=sa.Text(), nullable=True)
    op.drop_constraint(_MATRIX_ROOM, "sessions", type_="check")


def downgrade() -> None:
    op.create_check_constraint(_MATRIX_ROOM, "sessions", _MATRIX_ROOM_CHECK)
    op.alter_column("sessions", "surface", existing_type=sa.Text(), nullable=False)
