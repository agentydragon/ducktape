"""Retain runner attachment and confirmed EOF without rewriting archived Events."""

import sqlalchemy as sa
from alembic import op

revision = "0003_history_feed_state"
down_revision = "0002_session_open_reservations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("session_history", sa.Column("feed_state", sa.LargeBinary(), nullable=True))


def downgrade() -> None:
    op.drop_column("session_history", "feed_state")
