"""The runner's Session ID is the Session ID, so the registry keeps no separate runner locator."""

import sqlalchemy as sa
from alembic import op

revision = "0010_drop_session_runner_locator"
down_revision = "0009_drop_runner_connection"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("ux_session_runner_locator", table_name="session")
    op.drop_column("session", "runner_session_id")


def downgrade() -> None:
    op.add_column("session", sa.Column("runner_session_id", sa.String(), nullable=True))
    # A downgraded gateway resolves runner Sessions through the locator; every Session's is its ID.
    op.execute("UPDATE session SET runner_session_id = id::text")
    op.create_index(
        "ux_session_runner_locator",
        "session",
        ["sandbox_namespace", "sandbox_name", "sandbox_uid", "runner_session_id"],
        unique=True,
        postgresql_nulls_not_distinct=True,
        postgresql_where=sa.text("runner_session_id IS NOT NULL"),
    )
