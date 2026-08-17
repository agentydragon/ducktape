"""`sessions.status` admits `idle`, a release before anything writes it.

An idle session is one that exists and holds no sandbox, so a room nobody is speaking in stops
paying for one. This migration is only the schema half: the member and the `CHECK` widen here, the
writer lands next release.

**The split is what makes it safe.** `TextBackedStrEnumColumn` parses `sessions.status`, so a
replica on the previous image raises on a value it cannot name rather than degrading, and old and
new replicas share the schema for the length of a `maxUnavailable: 0` roll. Widening admits a value
nothing writes; writing it before every replica parses it is an outage.

The reverse direction needs nothing: every status the previous image writes is still admitted, and
no row holds `idle` for it to read.

Revision ID: 0054
Revises: 0052
"""

from __future__ import annotations

from alembic import op

revision: str = "0054"
down_revision: str | None = "0052"
branch_labels: str | None = None
depends_on: str | None = None

_TABLE = "sessions"
_CONSTRAINT = "ck_sessions_status"
_WITHOUT_IDLE = "status IN ('provisioning','ready','responding','closing','closed','failed')"
_WITH_IDLE = "status IN ('idle','provisioning','ready','responding','closing','closed','failed')"


def upgrade() -> None:
    op.drop_constraint(_CONSTRAINT, _TABLE, type_="check")
    op.create_check_constraint(_CONSTRAINT, _TABLE, _WITH_IDLE)


def downgrade() -> None:
    op.drop_constraint(_CONSTRAINT, _TABLE, type_="check")
    op.create_check_constraint(_CONSTRAINT, _TABLE, _WITHOUT_IDLE)
