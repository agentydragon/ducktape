"""Make the chat session lease required, now that nothing can write a row without one.

Revision ID: 0029
Revises: 0028
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0029"
down_revision: str | None = "0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Enforce at the database what the sweep already assumes.

    A live session with no lease is unreclaimable — the sweep looks for a lease that has
    passed, and a missing one never does. 0028 repaired the rows that were already in that
    state; this stops new ones from being created, which is the part a code review cannot
    guarantee on its own.

    Safe as a plain `SET NOT NULL` rather than an expand/contract only because of the release
    ordering: every replica able to insert here has been writing the column since 0027 shipped,
    and 0028 removed the historical nulls. Landing this in the *same* release as the backfill
    would not have been safe, since a replica from the previous image could insert a null
    between the migration and the end of the roll.
    """
    op.alter_column("claude_chat_sessions", "lease_expires_at", nullable=False)
    # The predicate's null check is now always true, so it only obscures what the index is for:
    # finding live sessions by lease, which is exactly the sweep's query.
    op.drop_index("idx_claude_chat_sessions_expired_lease", table_name="claude_chat_sessions")
    op.create_index(
        "idx_claude_chat_sessions_expired_lease",
        "claude_chat_sessions",
        ["lease_expires_at"],
        postgresql_where=sa.text("status IN ('provisioning','ready','responding')"),
    )


def downgrade() -> None:
    op.drop_index("idx_claude_chat_sessions_expired_lease", table_name="claude_chat_sessions")
    op.create_index(
        "idx_claude_chat_sessions_expired_lease",
        "claude_chat_sessions",
        ["lease_expires_at"],
        postgresql_where=sa.text("lease_expires_at IS NOT NULL AND status IN ('provisioning','ready','responding')"),
    )
    op.alter_column("claude_chat_sessions", "lease_expires_at", nullable=True)
