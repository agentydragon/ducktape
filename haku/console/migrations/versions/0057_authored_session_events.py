"""Give the `authored` arm of `session_events` somewhere to write: no turn, and two more kinds.

`EventProvenance.AUTHORED` has had no writer since `0052`. The frame log is the record of
runner↔console traffic and nothing else, and a lease changing hands crosses no wire, so it is a row
here rather than a frame. Two facts get a writer with this migration — a replica taking a session
over, and a lease lapsing past the adoption grace — and neither has a turn to name: the second
exists precisely to record a session that died before it ever reached one.

Every change here is a relaxation:

- **`turn_id` becomes nullable.** A projected event still names the turn whose fold produced it;
  an authored one names the session and nothing else.
- **`ck_session_events_provenance_frames` gains the turn**: required on the `frame_range` arm,
  optional on `authored`.
- **`ck_session_events_kind` gains `session_adopted` and `lease_expired`.**

**Additive, so it is safe for the length of a roll.** Dropping a NOT NULL and widening two CHECKs
forbids nothing a replica on the previous image writes. What it cannot do is *read* an authored row
— nothing on that image selects `session_events` except the transcript's tool-call view, which
filters to the two tool kinds.

Revision ID: 0057
Revises: 0056
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0057"
down_revision: str | None = "0056"
branch_labels: str | None = None
depends_on: str | None = None

_TABLE = "session_events"
_FRAMES = "ck_session_events_provenance_frames"
_KIND = "ck_session_events_kind"

# The spellings are written out rather than imported from the ORM, for the reason `0041` gives: a
# migration is a point-in-time statement about the database.
_CONVERSATION_KINDS = (
    "'message_completed','reasoning','tool_call_started','tool_call_completed','activity_started','activity_completed'"
)
_AUTHORED_KINDS = "'session_adopted','lease_expired'"

_FRAMES_WITHOUT_TURN = (
    "(provenance = 'frame_range') = (source_first_frame_seq IS NOT NULL) "
    "AND (source_first_frame_seq IS NULL) = (source_last_frame_seq IS NULL) "
    "AND (source_first_frame_seq IS NULL OR source_first_frame_seq <= source_last_frame_seq)"
)
_FRAMES_WITH_TURN = f"{_FRAMES_WITHOUT_TURN} AND (provenance <> 'frame_range' OR turn_id IS NOT NULL)"


def upgrade() -> None:
    op.alter_column(_TABLE, "turn_id", existing_type=postgresql.UUID(as_uuid=True), nullable=True)
    op.drop_constraint(_FRAMES, _TABLE, type_="check")
    op.create_check_constraint(_FRAMES, _TABLE, _FRAMES_WITH_TURN)
    op.drop_constraint(_KIND, _TABLE, type_="check")
    op.create_check_constraint(_KIND, _TABLE, f"kind IN ({_CONVERSATION_KINDS},{_AUTHORED_KINDS})")


def downgrade() -> None:
    # A turn-less row cannot survive `turn_id` becoming NOT NULL, and nothing re-derives one: it
    # was never in the frames, which is what `authored` means.
    op.execute(sa.text(f"DELETE FROM {_TABLE} WHERE turn_id IS NULL"))
    op.drop_constraint(_KIND, _TABLE, type_="check")
    op.create_check_constraint(_KIND, _TABLE, f"kind IN ({_CONVERSATION_KINDS})")
    op.drop_constraint(_FRAMES, _TABLE, type_="check")
    op.create_check_constraint(_FRAMES, _TABLE, _FRAMES_WITHOUT_TURN)
    op.alter_column(_TABLE, "turn_id", existing_type=postgresql.UUID(as_uuid=True), nullable=False)
