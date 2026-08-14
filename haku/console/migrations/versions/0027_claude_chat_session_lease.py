"""A live chat session is held by a lease, so a dead replica's session can be reclaimed.

Revision ID: 0027
Revises: 0026
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0027"
down_revision: str | None = "0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Nullable, so this is additive: a replica running the previous image keeps writing rows
    # without it for the length of a roll, and those simply have no lease until their owner
    # renews one. The sweep only ever acts on a lease that exists and has passed.
    op.add_column("claude_chat_sessions", sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index(
        "idx_claude_chat_sessions_expired_lease",
        "claude_chat_sessions",
        ["lease_expires_at"],
        postgresql_where=sa.text("lease_expires_at IS NOT NULL AND status IN ('provisioning','ready','responding')"),
    )


def downgrade() -> None:
    op.drop_index("idx_claude_chat_sessions_expired_lease", table_name="claude_chat_sessions")
    op.drop_column("claude_chat_sessions", "lease_expires_at")
