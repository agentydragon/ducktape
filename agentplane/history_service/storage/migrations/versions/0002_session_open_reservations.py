"""Reserve Service-owned Session IDs before opening a runner session."""

import sqlalchemy as sa
from alembic import op

revision = "0002_session_open_reservations"
down_revision = "0001_session_history"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("session_history", "runner_session_id", nullable=True)
    for name, column_type in (
        ("caller_namespace", sa.String()),
        ("caller_name", sa.String()),
        ("open_key", sa.String()),
        ("open_request", sa.LargeBinary()),
        ("launch_spec", sa.LargeBinary()),
    ):
        op.add_column("session_history", sa.Column(name, column_type, nullable=True))
    op.create_index(
        "ux_history_runner_locator",
        "session_history",
        ["sandbox_namespace", "sandbox_name", "sandbox_uid", "runner_session_id"],
        unique=True,
        postgresql_nulls_not_distinct=True,
        postgresql_where=sa.text("runner_session_id IS NOT NULL"),
    )
    op.create_index(
        "ux_history_open_key",
        "session_history",
        ["caller_namespace", "caller_name", "sandbox_namespace", "sandbox_name", "sandbox_uid", "open_key"],
        unique=True,
        postgresql_where=sa.text("open_key IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ux_history_open_key", table_name="session_history")
    op.drop_index("ux_history_runner_locator", table_name="session_history")
    op.drop_column("session_history", "launch_spec")
    op.drop_column("session_history", "open_request")
    op.drop_column("session_history", "open_key")
    op.drop_column("session_history", "caller_name")
    op.drop_column("session_history", "caller_namespace")
    op.alter_column("session_history", "runner_session_id", nullable=False)
