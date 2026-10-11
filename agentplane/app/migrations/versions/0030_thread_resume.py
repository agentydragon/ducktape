"""Keep a confirmed resume on the app's own Thread row, not in a fold table.

The app no longer writes `thread_history_summary`; the masks recorded there move with it.
"""

from alembic import op

revision = "0030_thread_resume"
down_revision = "0029_event_log_copies_drop"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Earlier-migration replay tests can stamp an older head on an otherwise current schema.
    op.execute("ALTER TABLE thread ADD COLUMN IF NOT EXISTS resumed_after_cursor bigint")
    op.execute(
        "INSERT INTO thread (id, resumed_after_cursor)"
        " SELECT summary.thread_id, summary.resumed_after_cursor FROM thread_history_summary AS summary"
        " JOIN event_log ON event_log.id = summary.thread_id"
        " WHERE summary.resumed_after_cursor IS NOT NULL"
        " ON CONFLICT (id) DO UPDATE SET resumed_after_cursor = EXCLUDED.resumed_after_cursor"
    )


def downgrade() -> None:
    op.drop_column("thread", "resumed_after_cursor")
