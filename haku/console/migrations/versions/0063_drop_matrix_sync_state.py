"""Drop `matrix_sync_state`. **Destructive.**

The contract half of `0060`, which copied its two columns into `matrix_access_token` and
`matrix_sync_watermark`. A later release stopped mapping the table, so nothing selects it any more.

**Gate this on the roll having converged** — every pod on an image at or after the unmapping, not on
a release having elapsed. An ORM-mapped table is named in every `SELECT` SQLAlchemy emits for it
whether or not any code reads the attribute, so a replica still on the mapping image would fail on
every statement the moment this runs.

The column types are spelled out below rather than imported from `0025`, for the reason `0041`
gives.

Revision ID: 0063
Revises: 0062
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0063"
down_revision: str | None = "0062"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.drop_table("matrix_sync_state")


def downgrade() -> None:
    op.create_table(
        "matrix_sync_state",
        sa.Column("user_id", sa.Text(), primary_key=True),
        sa.Column("access_token", sa.Text(), nullable=True),
        sa.Column("next_batch", sa.Text(), nullable=True),
    )
    # `0060`'s downgrade drops the two tables believing `matrix_sync_state` still holds what was
    # copied out of it. That is only true if this puts it back, so rejoin the split rows here — a
    # user with a row in one table and not the other gets the NULL the column had before `0060`.
    op.execute(
        sa.text(
            "INSERT INTO matrix_sync_state (user_id, access_token, next_batch) "
            "SELECT COALESCE(t.user_id, w.user_id), t.access_token, w.next_batch "
            "FROM matrix_access_token t "
            "FULL OUTER JOIN matrix_sync_watermark w ON w.user_id = t.user_id"
        )
    )
