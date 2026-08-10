"""Matrix sync watermark and cached bot access token.

Revision ID: 0025
Revises: 0024
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0025"
down_revision: str | None = "0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "matrix_sync_state",
        sa.Column("user_id", sa.Text(), primary_key=True),
        sa.Column("access_token", sa.Text(), nullable=True),
        sa.Column("next_batch", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("matrix_sync_state")
