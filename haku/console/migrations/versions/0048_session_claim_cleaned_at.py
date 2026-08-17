"""Give sandbox-claim cleanup its own column instead of blanking the rendezvous credential.

`bridge_token_fingerprint = ''` meant "the claim is gone", so one credential column carried two
unrelated facts and encoded the second as the zero value of the first. `claim_cleaned_at` says it
directly, and the fingerprint goes back to meaning only what it verifies.

**Additive on purpose.** `maxUnavailable: 0` keeps a replica on the previous image writing and
reading the blanked fingerprint for the length of the roll; the new column is nullable, so that
image's INSERT, which does not name it, still succeeds. Making `bridge_token_fingerprint` nullable
would be the destructive half — an old replica maps it NOT NULL — and this release does not do it.

The backfill takes each already-cleaned row's `updated_at`, which is the instant the transaction
that blanked the fingerprint stamped rather than the wall clock of this migration. Rows still
holding a real fingerprint are uncleaned and stay NULL, so the sweep's candidate set is unchanged.

Revision ID: 0048
Revises: 0047
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0048"
down_revision: str | None = "0047"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("sessions", sa.Column("claim_cleaned_at", sa.DateTime(timezone=True), nullable=True))
    op.execute(sa.text("UPDATE sessions SET claim_cleaned_at = updated_at WHERE bridge_token_fingerprint = ''::bytea"))


def downgrade() -> None:
    op.drop_column("sessions", "claim_cleaned_at")
