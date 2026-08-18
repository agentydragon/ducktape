"""Drop `sessions.surface` and `ck_sessions_surface`, which the ORM no longer maps. **Destructive.**

The last of the three steps the README's Perimeter / deploy section requires for a mapped column:
`0064` gave a conversation its channels through `chat_attachment`, `0075` made this column nullable
so the release that stops naming it does not fail the first `INSERT` of its roll, #4350 stopped
mapping it, and this drops it.

Gated on that unmapping having converged, not on a release having elapsed: an ORM-mapped column is
named in every `SELECT` SQLAlchemy emits for `Session` whether or not any code reads the attribute,
so a replica still on the mapping image would fail on every session read the moment this runs — and
`maxUnavailable: 0` lets a stalled roll keep such a replica serving. Read both pods' image tags
before applying this, as `0068` and `0069` did.

`ck_sessions_surface` goes with the column and nothing else does: no index ever named it, and
`ck_sessions_matrix_room` — the only other constraint that did — was dropped by `0075`. Postgres
would drop the CHECK along with the column anyway; naming it keeps the downgrade symmetric.

Revision ID: 0081
Revises: 0080
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0081"
down_revision: str | None = "0080"
branch_labels: str | None = None
depends_on: str | None = None

_SURFACE = "ck_sessions_surface"
_SURFACE_CHECK = "surface IN ('spa','matrix')"


def upgrade() -> None:
    op.drop_constraint(_SURFACE, "sessions", type_="check")
    op.drop_column("sessions", "surface")


def downgrade() -> None:
    """Puts the column back empty, which is what an earlier image expects to find.

    Nothing backfills it, and the CHECK admits that: `NULL IN (...)` is unknown rather than false,
    so every restored row passes. An image that selects this column names it because it is mapped,
    not because anything reads the value — which channels hold a conversation is `chat_attachment`.
    """
    op.add_column("sessions", sa.Column("surface", sa.Text(), nullable=True))
    op.create_check_constraint(_SURFACE, "sessions", _SURFACE_CHECK)
