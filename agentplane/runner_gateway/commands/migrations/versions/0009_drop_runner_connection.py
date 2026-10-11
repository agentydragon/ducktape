"""Every runner connection carries every control operation, so nothing records which ones it agreed."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0009_drop_runner_connection"
down_revision = "0008_drop_session_feed"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_table("runner_connection")


def downgrade() -> None:
    # Empty: a downgraded gateway reads no row as no operation agreed yet.
    op.create_table(
        "runner_connection",
        sa.Column("sandbox_uid", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("pod_uid", sa.String(), nullable=True),
        sa.Column("connection_epoch", sa.BigInteger(), nullable=True),
        sa.Column("owner_replica", sa.String(), nullable=True),
        sa.Column("runner_boot_id", sa.String(), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("operations", postgresql.ARRAY(sa.String()), nullable=False),
        sa.CheckConstraint("connection_epoch > 0", name="connection_epoch_positive"),
    )
