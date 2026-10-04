"""Remove pause without unexpectedly restarting stopped subscriptions."""

import sqlalchemy as sa
from alembic import op

revision = "0003_remove_pause"
down_revision = "0002_idempotency_key"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("UPDATE subscription SET cancelled = true, version = version + 1 WHERE paused AND NOT cancelled")
    op.drop_column("subscription", "paused")


def downgrade() -> None:
    # Cancellation is retained: rollback must not restart stopped subscriptions either.
    op.add_column("subscription", sa.Column("paused", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.alter_column("subscription", "paused", server_default=None)
