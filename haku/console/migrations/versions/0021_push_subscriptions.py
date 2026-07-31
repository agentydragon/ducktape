"""Add browser Web Push subscriptions.

Revision ID: 0021
Revises: 0020
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "push_subscriptions",
        sa.Column("endpoint", sa.Text(), nullable=False),
        sa.Column(
            "operator_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("operators.operator_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("p256dh", sa.Text(), nullable=False),
        sa.Column("auth", sa.Text(), nullable=False),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_failure_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("endpoint"),
    )
    op.create_index("idx_push_subscriptions_operator_id", "push_subscriptions", ["operator_id"])


def downgrade() -> None:
    op.drop_index("idx_push_subscriptions_operator_id", table_name="push_subscriptions")
    op.drop_table("push_subscriptions")
