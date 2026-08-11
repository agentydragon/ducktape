"""Give every session written before 0027 a lease, so an orphan is reclaimable.

Revision ID: 0028
Revises: 0027
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

revision: str = "0028"
down_revision: str | None = "0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Deliberately spelled here rather than imported from `claude_chat.LEASE_TTL`: a migration is a
# point-in-time statement about the database, and importing an application constant would make
# an already-applied migration change meaning when that constant is next tuned.
_LEASE_TTL_SECONDS = 90


def upgrade() -> None:
    """Backfill `lease_expires_at`, which 0027 could only add as nullable.

    `expire_stale_leases` reclaims a live session by looking for a lease that has passed. A row
    written before 0027 has no lease at all, so it is invisible to that sweep — permanently.
    That is not hypothetical: the Matrix session this whole mechanism was built for was already
    wedged in `responding` when 0027 shipped, and could never have been recovered by it.

    Live rows get one TTL of grace rather than an expired lease. A session whose replica is
    genuinely alive renews well inside that window and is unaffected; an orphan never renews and
    is reclaimed one TTL from now. Backfilling something already past would have briefly marked
    every healthy session dead, which is a worse bug than the one being fixed.

    Terminal rows get `updated_at`, which is when their lease effectively ended. They are not
    swept — the status filter excludes them — but the column is about to become `NOT NULL`, and
    a value that is honest about the row beats a placeholder.
    """
    op.execute(
        text(f"""
        UPDATE claude_chat_sessions
           SET lease_expires_at = CASE
                   WHEN status IN ('provisioning', 'ready', 'responding')
                   THEN now() + interval '{_LEASE_TTL_SECONDS} seconds'
                   ELSE updated_at
               END
         WHERE lease_expires_at IS NULL
        """)
    )


def downgrade() -> None:
    """Not reversible in any useful sense: which rows had no lease is not recorded."""
