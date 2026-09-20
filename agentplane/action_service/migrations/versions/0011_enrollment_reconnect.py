"""Retain the exact existing Connection and version selected in fresh OAuth consent."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0011_enrollment_reconnect"
down_revision = "0010_connection_enrollments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "connection_enrollment",
        sa.Column(
            "connection_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("external_connection.id"), nullable=True
        ),
    )
    op.add_column("connection_enrollment", sa.Column("connection_version", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("connection_enrollment", "connection_version")
    op.drop_column("connection_enrollment", "connection_id")
