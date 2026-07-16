"""Per-user streak and daily-bonus state (credit system v2 phase 2).

Revision ID: 0004
Revises: 0003
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "credit_state",
        sa.Column("user_id", sa.String(length=64), primary_key=True),
        sa.Column("streak_days", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_qualifying_date", sa.String(length=10), nullable=True),
        sa.Column("rest_days_used", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_first_bonus_date", sa.String(length=10), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("credit_state")
