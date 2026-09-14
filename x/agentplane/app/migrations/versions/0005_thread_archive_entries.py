"""Make the app's retained Thread history a source-neutral EventEntry archive."""

import sqlalchemy as sa
from alembic import op

revision = "0005_thread_archive_entries"
down_revision = "0004_thread_command_outbox"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # `0004` already cut over the runner protocol. The former copied rows use a runner cursor as
    # their primary key, so they cannot coexist with app-origin admission entries. This is a second
    # intentional hard cut: no reader or converter preserves the retired archive shape.
    op.execute("TRUNCATE TABLE event, feed_state")
    op.add_column("thread", sa.Column("archive_cursor", sa.BigInteger(), nullable=False, server_default=sa.text("0")))
    op.add_column("thread_runner_session", sa.Column("event_source_id", sa.Text()))
    op.add_column(
        "thread_runner_session",
        sa.Column("import_cursor", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
    )
    op.add_column("event", sa.Column("origin_source_id", sa.Text(), nullable=False))
    op.add_column("event", sa.Column("origin_sequence", sa.BigInteger(), nullable=False))
    op.create_unique_constraint(
        "event_thread_origin_key", "event", ["thread_id", "origin_source_id", "origin_sequence"]
    )


def downgrade() -> None:
    raise RuntimeError("0005_thread_archive_entries is a hard cutover with no old archive reader")
