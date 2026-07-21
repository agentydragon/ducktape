"""Add short-lived OAuth callback results for the console SPA.

Revision ID: 0018
Revises: 0017
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "oauth_connection_results",
        sa.Column("result_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("operator_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('success', 'error')", name="ck_oauth_connection_results_status"),
        sa.CheckConstraint("btrim(title) <> ''", name="ck_oauth_connection_results_title_nonempty"),
        sa.CheckConstraint("btrim(message) <> ''", name="ck_oauth_connection_results_message_nonempty"),
        sa.ForeignKeyConstraint(["operator_id"], ["operators.operator_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("result_id"),
    )
    op.create_index("idx_oauth_connection_results_operator", "oauth_connection_results", ["operator_id"])
    op.create_index("idx_oauth_connection_results_expires_at", "oauth_connection_results", ["expires_at"])


def downgrade() -> None:
    op.drop_index("idx_oauth_connection_results_expires_at", table_name="oauth_connection_results")
    op.drop_index("idx_oauth_connection_results_operator", table_name="oauth_connection_results")
    op.drop_table("oauth_connection_results")
