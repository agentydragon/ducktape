"""`sessions.status` stops admitting `idle`, which nothing ever wrote.

`0054` widened the CHECK a release ahead of a writer that would create a session holding no sandbox
and let a prompt buy it one. That design is not the one the console got: a conversation holds an
incoming prompt until a session provisions, so a session exists only where there is demand and is
created straight into `provisioning`.

**Narrowing is safe only because no writer exists on either side of the roll.** The console rolls
with `maxUnavailable: 0`, so a replica on the previous image serves against this schema for the
length of the roll — and that image's `SessionStatus` has an `idle` member no code path assigns.
Nothing can be rejected by the constraint that anything would have inserted, and no row holds `idle`
for the constraint to be added over.

The reverse direction re-admits a value nothing writes, which is what `0054` did.

Revision ID: 0077
Revises: 0075
"""

from __future__ import annotations

from alembic import op

revision: str = "0077"
down_revision: str | None = "0075"
branch_labels: str | None = None
depends_on: str | None = None

_TABLE = "sessions"
_CONSTRAINT = "ck_sessions_status"
_WITHOUT_IDLE = "status IN ('provisioning','ready','responding','closing','closed','failed')"
_WITH_IDLE = "status IN ('idle','provisioning','ready','responding','closing','closed','failed')"


def upgrade() -> None:
    op.drop_constraint(_CONSTRAINT, _TABLE, type_="check")
    op.create_check_constraint(_CONSTRAINT, _TABLE, _WITHOUT_IDLE)


def downgrade() -> None:
    op.drop_constraint(_CONSTRAINT, _TABLE, type_="check")
    op.create_check_constraint(_CONSTRAINT, _TABLE, _WITH_IDLE)
