"""Order the Thread list by when a Session's history began, which pages stably.

Only a fold from cursor 1 sets it, so rows the app projection folded earlier stay null.
"""

from alembic import op

revision = "0027_summary_first_event"
down_revision = "0026_fold_keys_drop"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Earlier-migration replay tests can stamp an older head on an otherwise current schema.
    op.execute("ALTER TABLE thread_history_summary ADD COLUMN IF NOT EXISTS first_event_at timestamptz")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_thread_history_summary_first_event"
        " ON thread_history_summary (first_event_at, thread_id)"
    )


def downgrade() -> None:
    op.drop_index("ix_thread_history_summary_first_event", table_name="thread_history_summary")
    op.drop_column("thread_history_summary", "first_event_at")
