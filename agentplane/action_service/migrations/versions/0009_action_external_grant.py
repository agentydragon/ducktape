"""Preserve immutable authenticated external grant evidence on each Action."""

from alembic import op
from sqlalchemy import Column
from sqlalchemy.dialects.postgresql import JSONB

revision = "0009_action_external_grant"
down_revision = "0008_external_connections"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("action_request", Column("external_grant", JSONB(none_as_null=True), nullable=True))


def downgrade() -> None:
    op.drop_column("action_request", "external_grant")
