"""The current runner channel connection per Sandbox."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002_runner_connection"
down_revision = "0001_command_admission"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "runner_connection",
        sa.Column("sandbox_uid", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("pod_uid", sa.String(), nullable=False),
        sa.Column("connection_epoch", sa.BigInteger(), nullable=False),
        sa.Column("owner_replica", sa.String(), nullable=False),
        sa.Column("runner_boot_id", sa.String(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("operations", postgresql.ARRAY(sa.String()), nullable=False),
        sa.CheckConstraint("connection_epoch > 0", name="connection_epoch_positive"),
    )


def downgrade() -> None:
    op.drop_table("runner_connection")
