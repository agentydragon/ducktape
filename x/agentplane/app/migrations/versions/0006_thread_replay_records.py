"""Persist the app-to-browser replay envelope without duplicating command or event payloads."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0006_thread_replay_records"
down_revision = "0005_thread_command_outbox"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "thread_record",
        sa.Column(
            "thread_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("thread.id", ondelete="CASCADE"), primary_key=True
        ),
        sa.Column("cursor", sa.BigInteger(), primary_key=True),
        sa.Column("command_id", sa.Text()),
        sa.Column("event_cursor", sa.BigInteger()),
        sa.Column(
            "runner_session_id", sa.Text(), sa.ForeignKey("thread_runner_session.runner_session_id", ondelete="CASCADE")
        ),
        sa.ForeignKeyConstraint(
            ["thread_id", "command_id"], ["thread_command.thread_id", "thread_command.command_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["thread_id", "event_cursor"], ["event.thread_id", "event.cursor"], ondelete="CASCADE"),
        sa.CheckConstraint(
            "(command_id IS NOT NULL AND event_cursor IS NULL AND runner_session_id IS NULL) OR "
            "(command_id IS NULL AND event_cursor IS NOT NULL AND runner_session_id IS NOT NULL)",
            name="thread_record_exactly_one_source",
        ),
    )


def downgrade() -> None:
    raise RuntimeError("0006_thread_replay_records cannot be downgraded without discarding replay history")
