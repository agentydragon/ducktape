"""Hard-cut stored runner observations to shared EventEntries and follow cursors."""

from alembic import op

revision = "0004_followable_event_entries"
down_revision = "0003_thread_harness"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Old rows contain the retired runner-local Event proto-JSON and Attached.last_sequence. There
    # is intentionally no reader or converter for them: protocol cutover starts a fresh archive.
    op.execute("TRUNCATE TABLE event, feed_state")
    op.alter_column("event", "sequence", new_column_name="cursor")


def downgrade() -> None:
    op.alter_column("event", "cursor", new_column_name="sequence")
