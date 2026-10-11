"""Order Session summaries by when their history began, which pages stably."""

from alembic import op

revision = "0006_summary_first_event"
down_revision = "0005_thread_fold_projection"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE thread_history_summary ADD COLUMN IF NOT EXISTS first_event_at timestamptz")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_thread_history_summary_first_event"
        " ON thread_history_summary (first_event_at, thread_id)"
    )


def downgrade() -> None:
    op.drop_index("ix_thread_history_summary_first_event", table_name="thread_history_summary")
    op.drop_column("thread_history_summary", "first_event_at")
