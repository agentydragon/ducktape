"""Store a transport replay ledger without conflating it with runner session timelines."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0005_thread_replay_records"
down_revision = "0004_thread_command_outbox"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # This is an intentional protocol cutover.  Old rows lack both the runner-session identity
    # and the app-persistence order required to replay them honestly, so do not invent either.
    op.execute("TRUNCATE TABLE thread CASCADE")

    op.add_column("thread", sa.Column("replay_cursor", sa.BigInteger(), nullable=False, server_default=sa.text("0")))
    op.add_column("event", sa.Column("runner_session_id", sa.Text(), nullable=False))
    op.drop_constraint("event_pkey", "event", type_="primary")
    op.create_primary_key("event_pkey", "event", ["runner_session_id", "sequence"])
    op.create_foreign_key(
        "event_runner_session_id_fkey",
        "event",
        "thread_runner_session",
        ["runner_session_id"],
        ["runner_session_id"],
        ondelete="CASCADE",
    )

    op.create_table(
        "thread_record",
        sa.Column(
            "thread_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("thread.id", ondelete="CASCADE"), primary_key=True
        ),
        sa.Column("replay_cursor", sa.BigInteger(), primary_key=True),
        sa.Column("command_id", sa.Text()),
        sa.Column("runner_session_id", sa.Text()),
        sa.Column("runner_sequence", sa.BigInteger()),
        sa.UniqueConstraint("thread_id", "command_id"),
        sa.UniqueConstraint("runner_session_id", "runner_sequence"),
        sa.CheckConstraint(
            "(command_id IS NOT NULL AND runner_session_id IS NULL AND runner_sequence IS NULL) "
            "OR (command_id IS NULL AND runner_session_id IS NOT NULL AND runner_sequence IS NOT NULL)",
            name="thread_record_has_one_source",
        ),
        sa.ForeignKeyConstraint(
            ["thread_id", "command_id"],
            ["thread_command.thread_id", "thread_command.command_id"],
            name="thread_record_command_fkey",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["runner_session_id", "runner_sequence"],
            ["event.runner_session_id", "event.sequence"],
            name="thread_record_runner_event_fkey",
            ondelete="CASCADE",
        ),
    )


def downgrade() -> None:
    raise RuntimeError("0005_thread_replay_records is a deliberate durable-protocol cutover")
