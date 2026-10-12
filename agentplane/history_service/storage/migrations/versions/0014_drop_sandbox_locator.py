"""Drop the Sandbox locator and runner session ID that rows the retired follower created carry.

This deletes them from those rows; take a CNPG `Backup` first.
"""

import sqlalchemy as sa
from alembic import op

revision = "0014_drop_sandbox_locator"
down_revision = "0013_feed_snapshot_public_ids"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("history_origin", "session_history", type_="check")
    op.drop_constraint("history_sandbox_complete", "session_history", type_="check")
    op.drop_index("ux_history_runner_locator", table_name="session_history")
    for column in ("runner_session_id", "sandbox_uid", "sandbox_name", "sandbox_namespace"):
        op.drop_column("session_history", column)


def downgrade() -> None:
    op.add_column("session_history", sa.Column("sandbox_namespace", sa.String(), nullable=True))
    op.add_column("session_history", sa.Column("sandbox_name", sa.String(), nullable=True))
    op.add_column("session_history", sa.Column("sandbox_uid", sa.Uuid(), nullable=True))
    op.add_column("session_history", sa.Column("runner_session_id", sa.String(), nullable=True))
    op.create_index(
        "ux_history_runner_locator",
        "session_history",
        ["sandbox_namespace", "sandbox_name", "sandbox_uid", "runner_session_id"],
        unique=True,
        postgresql_nulls_not_distinct=True,
        postgresql_where=sa.text("runner_session_id IS NOT NULL"),
    )
    op.create_check_constraint(
        "history_sandbox_complete", "session_history", "num_nulls(sandbox_namespace, sandbox_name) IN (0, 2)"
    )
    # Follower rows lost their locator and have no writer, so existing rows cannot satisfy the check.
    op.execute(
        "ALTER TABLE session_history ADD CONSTRAINT history_origin "
        "CHECK (sandbox_namespace IS NOT NULL OR writer_uid IS NOT NULL) NOT VALID"
    )
