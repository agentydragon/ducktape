"""Fold when each Session's harness last became idle.

Existing rows stay empty until the Session's next turn boundary.
"""

from alembic import op

revision = "0018_summary_idle_since"
down_revision = "0017_session_sandbox"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE thread_history_summary ADD COLUMN IF NOT EXISTS idle_since timestamptz")


def downgrade() -> None:
    op.drop_column("thread_history_summary", "idle_since")
