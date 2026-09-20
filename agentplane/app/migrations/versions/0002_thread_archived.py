"""Add Thread.archived, hiding a thread from the default listing without touching its events."""

import sqlalchemy as sa
from alembic import op

revision = "0002_thread_archived"
down_revision = "0001_trajectory_store"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("thread", sa.Column("archived", sa.Boolean(), nullable=False, server_default=sa.text("false")))


def downgrade() -> None:
    op.drop_column("thread", "archived")
