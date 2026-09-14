"""Persist a Thread target and planned first runner session after the command outbox."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0005_thread_start_request"
down_revision = "0004_thread_command_outbox"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "thread_start_request",
        # This is the caller-minted durable product Thread id, not a new request/session identity.
        sa.Column("thread_id", postgresql.UUID(as_uuid=True), nullable=False),
        # An existing target is pinned directly on Thread. A new target records only immutable
        # creation desired state until its reconciler binds the concrete Sandbox to that Thread.
        sa.Column("sandbox_creation", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.Column("runner_session_id", sa.Text(), nullable=False),
        sa.Column("session_spec", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["thread_id"], ["thread.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("thread_id"),
        sa.UniqueConstraint("runner_session_id"),
    )


def downgrade() -> None:
    raise RuntimeError("Agentplane does not support downgrading durable Thread state")
