"""Drop `subscription.creator` and `github_delivery.received_at`, which nothing reads.

Dropping them discards their data. Downgrade re-adds both columns nullable and empty.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0010_drop_write_only_columns"
down_revision = "0009_github_issue_subject"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("subscription", "creator")
    op.drop_column("github_delivery", "received_at")


def downgrade() -> None:
    op.add_column("github_delivery", sa.Column("received_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("subscription", sa.Column("creator", postgresql.JSONB(), nullable=True))
