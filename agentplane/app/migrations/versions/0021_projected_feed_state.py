"""Retain lifecycle beside the projection; never rewrite raw event history."""

from alembic import op

revision = "0021_projected_feed_state"
down_revision = "0020_thread_history_summary"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE thread_history_summary ADD COLUMN IF NOT EXISTS attached jsonb")
    op.execute('ALTER TABLE thread_history_summary ADD COLUMN IF NOT EXISTS "end" jsonb')
    op.execute("ALTER TABLE thread_history_summary ADD COLUMN IF NOT EXISTS resumed_after_cursor bigint")
    # Covers an already-fenced installation without touching Event payloads.
    op.execute("""
        UPDATE thread_history_summary AS summary
        SET attached = feed.attached, "end" = feed."end"
        FROM feed_state AS feed
        WHERE summary.thread_id = feed.thread_id AND summary.attached IS NULL
    """)


def downgrade() -> None:
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM event_log WHERE raw_ingestion_fenced_at_cursor IS NOT NULL) THEN
                RAISE EXCEPTION 'cannot remove lifecycle for service-projected Threads';
            END IF;
        END $$
    """)
    op.drop_column("thread_history_summary", "resumed_after_cursor")
    op.drop_column("thread_history_summary", "end")
    op.drop_column("thread_history_summary", "attached")
