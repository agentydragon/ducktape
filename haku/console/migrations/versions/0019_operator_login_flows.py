"""Add server-side operator browser login flow state.

Revision ID: 0019
Revises: 0018
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "operator_login_flows",
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("browser_binding", sa.Text(), nullable=False),
        sa.Column("return_to", sa.Text(), nullable=True),
        sa.Column("data", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("btrim(browser_binding) <> ''", name="ck_operator_login_flows_browser_binding_nonempty"),
        sa.PrimaryKeyConstraint("state"),
    )
    op.create_index("idx_operator_login_flows_expires_at", "operator_login_flows", ["expires_at"])


def downgrade() -> None:
    op.drop_index("idx_operator_login_flows_expires_at", table_name="operator_login_flows")
    op.drop_table("operator_login_flows")
