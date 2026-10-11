"""Drop the Sandbox Service registry's former columns: Open reservations and WatchSessions positions.

The registry lives in the Sandbox Service's own database; nothing has read these since.
"""

import sqlalchemy as sa
from alembic import op

revision = "0010_drop_registry_columns"
down_revision = "0009_history_rows_from_feed"
branch_labels = None
depends_on = None

COLUMNS = (
    ("caller_namespace", sa.String()),
    ("caller_name", sa.String()),
    ("open_key", sa.String()),
    ("open_request", sa.LargeBinary()),
    ("launch_spec", sa.LargeBinary()),
    ("change_position", sa.BigInteger()),
)


def upgrade() -> None:
    op.drop_index("ux_history_open_key", table_name="session_history")
    op.drop_index("ux_history_change_position", table_name="session_history")
    for name, _ in COLUMNS:
        op.drop_column("session_history", name)
    op.execute(sa.schema.DropSequence(sa.Sequence("session_change_position")))


def downgrade() -> None:
    op.execute(sa.schema.CreateSequence(sa.Sequence("session_change_position")))
    for name, column_type in COLUMNS:
        op.add_column("session_history", sa.Column(name, column_type, nullable=True))
    op.create_index("ux_history_change_position", "session_history", ["change_position"], unique=True)
    op.create_index(
        "ux_history_open_key",
        "session_history",
        ["caller_namespace", "caller_name", "sandbox_namespace", "sandbox_name", "sandbox_uid", "open_key"],
        unique=True,
        postgresql_where=sa.text("open_key IS NOT NULL"),
    )
