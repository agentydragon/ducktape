"""A session records how far its frames have been projected.

`sessions.projected_frame_seq` is the durable per-session position the fold resumes from: the
`frame_seq` of the last frame whose projected effects — the message rows, the room's outbox row,
the turn's state — are committed. It is written in the same transaction as those effects, which is
what makes them exactly-once.

**Additive, and safe for the length of a roll.** A replica on the previous image neither selects nor
writes this column, and it is nullable with no default, so nothing that image writes has to fill it.

**Deliberately not backfilled.** A value would be a claim about how far a *previous* holder's
projection got, and no query can answer that. Backfilling `max(frame_seq)` would assert every
recorded frame had landed and lose a turn's ending; backfilling `0` would re-project frames whose
effects are already durable, duplicating a message and a room reply. So NULL means "no cursor here",
and `adopt_open_turn` reads the frames itself for such a session. That population empties on its
own: `session_ttl_seconds` is 7200.

Revision ID: 0051
Revises: 0050
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0051"
down_revision: str | None = "0050"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("sessions", sa.Column("projected_frame_seq", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    op.drop_column("sessions", "projected_frame_seq")
