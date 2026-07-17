"""Per-Operator external provider connections (Google today).

Replaces Airlock's brokered ``haku_console_google`` access token with Haku-owned,
per-Operator OAuth connections: private refresh-token storage plus short-lived flow state.

Revision ID: 0011
Revises: 0010
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "provider_connections",
        sa.Column("operator_id", UUID(as_uuid=True), primary_key=True),
        sa.Column("provider", sa.String(), primary_key=True),
        sa.Column("connection_id", UUID(as_uuid=True), nullable=False),
        sa.Column("token_revision", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("access_token", sa.Text(), nullable=False),
        sa.Column("refresh_token", sa.Text(), nullable=True),
        sa.Column("token_type", sa.Text(), nullable=False),
        sa.Column("scope", sa.Text(), nullable=True),
        sa.Column("token_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["operator_id"], ["operators.operator_id"], name="fk_provider_connections_operator", ondelete="CASCADE"
        ),
        sa.UniqueConstraint("connection_id", name="uq_provider_connections_connection_id"),
    )
    op.create_index("idx_provider_connections_operator", "provider_connections", ["operator_id"])

    op.create_table(
        "provider_connection_flows",
        sa.Column("state", sa.Text(), primary_key=True),
        sa.Column("operator_id", UUID(as_uuid=True), nullable=False),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("redirect_uri", sa.Text(), nullable=False),
        sa.Column("code_verifier", sa.Text(), nullable=False),
        sa.Column("scope", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["operator_id"], ["operators.operator_id"], name="fk_provider_connection_flows_operator", ondelete="CASCADE"
        ),
    )
    op.create_index("idx_provider_connection_flows_operator", "provider_connection_flows", ["operator_id"])
    op.create_index("idx_provider_connection_flows_expires_at", "provider_connection_flows", ["expires_at"])


def downgrade() -> None:
    op.drop_table("provider_connection_flows")
    op.drop_table("provider_connections")
