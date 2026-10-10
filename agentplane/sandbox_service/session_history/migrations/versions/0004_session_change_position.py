"""Give every Session a commit-ordered position in the WatchSessions feed."""

import sqlalchemy as sa
from alembic import op

revision = "0004_session_change_position"
down_revision = "0003_history_feed_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(sa.schema.CreateSequence(sa.Sequence("session_change_position")))
    op.add_column("session_history", sa.Column("change_position", sa.BigInteger(), nullable=True))
    # Existing Sessions enter the feed once each, in an arbitrary but fixed order.
    op.execute("UPDATE session_history SET change_position = nextval('session_change_position')")
    op.alter_column("session_history", "change_position", nullable=False)
    op.create_index("ux_history_change_position", "session_history", ["change_position"], unique=True)


def downgrade() -> None:
    op.drop_index("ux_history_change_position", table_name="session_history")
    op.drop_column("session_history", "change_position")
    op.execute(sa.schema.DropSequence(sa.Sequence("session_change_position")))
