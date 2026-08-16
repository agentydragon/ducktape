"""A session records how far its frames have been projected.

`sessions.projected_frame_seq` is the durable per-session position the fold resumes from: the
`frame_seq` of the last frame whose projected effects — the message rows, the room's outbox row,
the turn's state — are committed. It is written in the same transaction as those effects, which is
what makes them exactly-once (<../../../plans/chat_runtime_projection.md> § The shape).

**Additive, and safe for the length of a roll.** A replica on the previous image (README §
Perimeter / deploy) neither selects nor writes this column, so its INSERTs and UPDATEs are
unaffected; the column is nullable with no default, so nothing it writes has to fill it.

**Deliberately not backfilled.** A value would be a claim about how far a *previous* holder's
projection got, and no query can answer that: the effects it left behind are message rows and
outbox rows, which say what was projected but not that nothing after them was. Backfilling
`max(frame_seq)` would assert every recorded frame had landed and lose a turn's ending; backfilling
`0` would assert none had and re-project frames whose effects are already durable, duplicating a
message and a room reply. So NULL means "no cursor here", and `adopt_open_turn` reads the frames
itself for such a session — which is exactly what it did before this column existed. Every session
predating this release is in that population, and it empties on its own: `session_ttl_seconds` is
7200, so no session that can still acquire a frame is cursor-less two hours after this ships.

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
