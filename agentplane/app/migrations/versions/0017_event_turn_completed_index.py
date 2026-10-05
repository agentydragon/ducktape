"""Index each thread's completed turns, so the thread list reads the newest without walking the log."""

import sqlalchemy as sa
from alembic import op

revision = "0017_event_turn_completed_index"
down_revision = "0016_thread_payload_chunk_json"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Idempotent: test_database_migrate rewinds a fully migrated database to 0015 and migrates it forward.
    op.create_index(
        "ix_event_thread_turn_completed",
        "event",
        ["thread_id", "cursor"],
        postgresql_where=sa.text("kind = 'turn_completed'"),
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_index("ix_event_thread_turn_completed", table_name="event")
