"""Permit database grants whose validity has no configured end.

An absent ``expires_at`` is a permanent grant, not a distinct grant source or lifecycle
variant. It remains subject to the ordinary ``ended_at`` end fact.

Revision ID: 0123
Revises: 0122
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0123"
down_revision: str | None = "0122"
branch_labels: str | None = None
depends_on: str | None = None

_TABLES = ("kubernetes_grants", "http_grants")


def upgrade() -> None:
    for table in _TABLES:
        op.drop_constraint(f"ck_{table}_expiration_after_creation", table, type_="check")
        op.alter_column(table, "expires_at", existing_type=sa.DateTime(timezone=True), nullable=True)
        op.create_check_constraint(
            f"ck_{table}_expiration_after_creation", table, "expires_at IS NULL OR expires_at > created_at"
        )


def downgrade() -> None:
    for table in _TABLES:
        op.drop_constraint(f"ck_{table}_expiration_after_creation", table, type_="check")
        op.alter_column(table, "expires_at", existing_type=sa.DateTime(timezone=True), nullable=False)
        op.create_check_constraint(f"ck_{table}_expiration_after_creation", table, "expires_at > created_at")
