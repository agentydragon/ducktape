"""An event the fold produced names the frames it was read from.

`0052` stated the union's internal consistency — frames present on exactly the `frame_range` arm —
and left which arm a *kind* may take to convention. `ConversationEventKind` is by definition what
folding a recorded frame produced, so the two are one rule, and the missing half is the one that
breaks asymmetrically: a writer that took the `authored` arm under a projected kind succeeds, and
the failure lands on the read, where `session_views._asked` raises on a tool call with no frame and
runs on every `SessionStore.get`. One such row makes a whole session's transcript unreadable.

**Nothing on the previous image can write a row this rejects**, so it is safe for the length of a
`maxUnavailable: 0` roll. `x/session_events.row` is the only writer of these four kinds, and it
takes the arm from the event's own provenance; the sole adapter feeding it
(`x/claude_code/projection.py`) constructs a `FrameRange` on every event it emits, and
`conversation_events.Authored` has no producer at all. The other five kinds are written by
`authored`, `prompt_enqueued` and `turn_aborted`, which this leaves alone.

`NOT VALID` and then `VALIDATE CONSTRAINT` for the lock rather than for the rows: the add takes
`ACCESS EXCLUSIVE` and the scan takes only `SHARE UPDATE EXCLUSIVE`, so concurrent writers are not
held behind the table scan. Both run here — a constraint left unenforced would state the invariant
without holding it.

Revision ID: 0073
Revises: 0072
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0073"
down_revision: str | None = "0072"
branch_labels: str | None = None
depends_on: str | None = None

_TABLE = "session_events"
_CONSTRAINT = "ck_session_events_frame_derived_kinds"

# Written out rather than imported from the ORM, for the reason `0041` gives: a migration is a
# point-in-time statement about the database and must not change meaning when another file is
# edited.
_CHECK = (
    "provenance = 'frame_range' OR kind NOT IN "
    "('message_completed','reasoning','tool_call_started','tool_call_completed')"
)


def upgrade() -> None:
    op.execute(sa.text(f"ALTER TABLE {_TABLE} ADD CONSTRAINT {_CONSTRAINT} CHECK ({_CHECK}) NOT VALID"))
    op.execute(sa.text(f"ALTER TABLE {_TABLE} VALIDATE CONSTRAINT {_CONSTRAINT}"))


def downgrade() -> None:
    op.drop_constraint(_CONSTRAINT, _TABLE, type_="check")
