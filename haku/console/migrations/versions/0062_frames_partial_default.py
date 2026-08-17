"""`session_frames.partial` gets a server default, so unmapping it does not stop the log.

`0030` created the column `NOT NULL` with no default, which was right while every writer named it.
The last one, `_write_partial_frame`, is gone (#4230), and the column is on its way out:
<../../../plans/next_month.md> § 1 unmaps it in phase 2 and drops it in phase 3. SQLAlchemy names
only *mapped* columns in an `INSERT`, so the release that unmaps it would omit `partial` from every
`INSERT INTO session_frames` and Postgres would reject the first frame of the roll. That is the step
the three-release sequence was missing: a `NOT NULL` column an expand/contract is about to unmap
needs a default before it stops being named, not after.

**Additive, and safe for the length of a roll.** A default supplies a value only for a statement
that names no column, and the previous image names this one on every insert — writing an explicit
`false`, which a default does not contradict.

`false` is spelled out rather than taken from the ORM's `default=False`, for the reason `0041`
gives: a migration is a point-in-time statement about the database, and reaching into code that
moves would make an already-applied migration change meaning. Here the code it would reach into is
deleted one revision later.

Revision ID: 0062
Revises: 0061
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0062"
down_revision: str | None = "0061"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.alter_column(
        "session_frames", "partial", existing_type=sa.Boolean(), existing_nullable=False, server_default=sa.false()
    )


def downgrade() -> None:
    op.alter_column(
        "session_frames", "partial", existing_type=sa.Boolean(), existing_nullable=False, server_default=None
    )
