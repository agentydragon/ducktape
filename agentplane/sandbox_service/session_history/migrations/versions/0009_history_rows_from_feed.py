"""Let ingesters create history rows without a WatchSessions position.

The position now belongs to the Sandbox Service's own registry; existing rows keep theirs.
"""

from alembic import op

revision = "0009_history_rows_from_feed"
down_revision = "0008_delta_settlement"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("session_history", "change_position", nullable=True)


def downgrade() -> None:
    op.alter_column("session_history", "change_position", nullable=False)
