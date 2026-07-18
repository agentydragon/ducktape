"""The Operator's own Authentik token (offline_access), persisted for hostexec token exchange.

One row per Operator, captured at browser login and self-refreshed; the hostexec server exchanges it
for a short-lived per-host token so the operator acts under their own Authentik identity.

Revision ID: 0012
Revises: 0011
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "operator_authentik_tokens",
        sa.Column("operator_id", UUID(as_uuid=True), primary_key=True),
        sa.Column("token_revision", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("access_token", sa.Text(), nullable=False),
        sa.Column("refresh_token", sa.Text(), nullable=True),
        sa.Column("token_type", sa.Text(), nullable=False),
        sa.Column("scope", sa.Text(), nullable=True),
        sa.Column("token_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["operator_id"], ["operators.operator_id"], name="fk_operator_authentik_tokens_operator", ondelete="CASCADE"
        ),
    )


def downgrade() -> None:
    op.drop_table("operator_authentik_tokens")
