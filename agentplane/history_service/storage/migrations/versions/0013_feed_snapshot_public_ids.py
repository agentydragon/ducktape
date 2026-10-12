"""Name every stored runner snapshot by its public Session ID.

Rows the retired follower created stored their feed snapshot, and the fold copied it into the
summary, under the runner's own session ID.
"""

from alembic import op

revision = "0013_feed_snapshot_public_ids"
down_revision = "0012_drop_resumed_after_cursor"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # A parser merges a repeated embedded message field and keeps the last value of a singular
    # scalar, so appending `attached { session_id: <id> }` (field 1 in both `SessionFeedState` and
    # `Attached`; a UUID's text is 36 bytes) replaces only the snapshot's session ID.
    op.execute(
        r"""
        UPDATE session_history
        SET feed_state = feed_state || '\x0a260a24'::bytea || convert_to(id::text, 'UTF8')
        WHERE feed_state IS NOT NULL AND runner_session_id IS NOT NULL AND runner_session_id <> id::text
        """
    )
    op.execute(
        """
        UPDATE thread_history_summary
        SET attached = jsonb_set(attached, '{sessionId}', to_jsonb(thread_id::text))
        WHERE attached IS NOT NULL AND attached->>'sessionId' IS DISTINCT FROM thread_id::text
        """
    )


def downgrade() -> None:
    """Every earlier revision accepts a snapshot under the public Session ID."""
