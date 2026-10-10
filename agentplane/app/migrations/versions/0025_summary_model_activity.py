"""Keep the model-activity projection on the summary, which moves with the fold.

`event_log.last_model_activity_at` stays until the event_log launch copies are dropped, so a
replica still on the previous image keeps writing a column that exists.
"""

from alembic import op

revision = "0025_summary_model_activity"
down_revision = "0024_retire_app_raw_history"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE thread_history_summary ADD COLUMN last_model_activity_at timestamptz")
    op.execute("""
        UPDATE thread_history_summary AS summary
        SET last_model_activity_at = log.last_model_activity_at
        FROM event_log AS log
        WHERE summary.thread_id = log.id
    """)


def downgrade() -> None:
    op.drop_column("thread_history_summary", "last_model_activity_at")
