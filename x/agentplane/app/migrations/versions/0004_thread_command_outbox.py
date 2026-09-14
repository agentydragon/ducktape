"""Separate durable product Threads from runner sessions and persist their command outbox."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0004_thread_command_outbox"
down_revision = "0003_thread_harness"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "thread_runner_session",
        sa.Column(
            "thread_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("thread.id", ondelete="CASCADE"), primary_key=True
        ),
        sa.Column("sandbox", sa.Text(), primary_key=True),
        sa.Column("runner_session_id", sa.Text(), primary_key=True),
        # Historical Thread rows predate UID persistence. New associations write it when known.
        sa.Column("sandbox_uid", postgresql.UUID(as_uuid=True)),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("sandbox", "runner_session_id"),
    )
    op.execute(
        """
        INSERT INTO thread_runner_session (thread_id, sandbox, runner_session_id, sandbox_uid, active, created_at)
        SELECT id, sandbox, session_id, NULL, true, created_at
        FROM thread
        """
    )
    op.create_index(
        "thread_runner_session_one_active",
        "thread_runner_session",
        ["thread_id"],
        unique=True,
        postgresql_where=sa.text("active"),
    )
    op.create_table(
        "thread_command",
        sa.Column(
            "thread_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("thread.id", ondelete="CASCADE"), primary_key=True
        ),
        sa.Column("command_id", sa.Text(), primary_key=True),
        sa.Column("ordinal", sa.BigInteger(), nullable=False),
        sa.Column("command", postgresql.JSONB(), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("thread_id", "ordinal"),
    )
    op.drop_constraint("thread_sandbox_session_id_key", "thread", type_="unique")
    op.drop_column("thread", "session_id")
    op.drop_column("thread", "sandbox")


def downgrade() -> None:
    raise RuntimeError("0004_thread_command_outbox cannot be downgraded without discarding durable command intent")
