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
            "thread_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("thread.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("runner_session_id", sa.Text(), primary_key=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.execute(
        """
        INSERT INTO thread_runner_session (thread_id, runner_session_id, active, created_at)
        SELECT id, session_id, true, created_at
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
    # Existing Threads already name their static Sandbox; only the Kubernetes object UID is new.
    op.add_column("thread", sa.Column("sandbox_uid", postgresql.UUID(as_uuid=True)))
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
    op.alter_column("thread", "sandbox", existing_type=sa.Text(), nullable=True)


def downgrade() -> None:
    raise RuntimeError("0004_thread_command_outbox cannot be downgraded without discarding durable command intent")
