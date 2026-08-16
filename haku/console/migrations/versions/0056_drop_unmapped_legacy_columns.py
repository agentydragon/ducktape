"""Drop the two columns the ORM stopped mapping. **Destructive.**

The contract half of `0047` and `0049`, and the phase after the release that unmapped both columns
(#4193). What each one held is already beside it:

- `session_messages.tool_uses` is Claude's wire spelling of a message's calls, which `0047` rewrote
  key by key into `tool_calls`. Every row that had calls has them there in the vocabulary the model
  validates; `0047` left the original exactly as it was so a replica on the previous image could
  keep selecting it.
- `session_turns.usage` is Claude's own `usage` sub-object, which `0049` read into the neutral
  counters beside it. The payload itself is not lost with the column — the whole `result` frame is
  in `session_frames` verbatim and the turn's frame range points at it (console `x/README.md`
  § The payload is evidence).

**Gate this on the roll having converged**, as `0041` does: dropping a column a serving replica
still names in its `SELECT` breaks that pod, and `maxUnavailable: 0` means a stalled roll can leave
an old replica serving long after its release shipped. Checked before this landed — both
`haku-console` pods on `devel-20260816212452-128f7ae`, one tag, and `d234a79c11` (the release that
unmapped the columns) an ancestor of `128f7ae`.

The column names are spelled out here rather than imported from the ORM, for the reason `0041`
gives: a migration is a point-in-time statement about the database, so it must not change meaning
when another file is edited.

Revision ID: 0056
Revises: 0055
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0056"
down_revision: str | None = "0055"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.drop_column("session_messages", "tool_uses")
    op.drop_column("session_turns", "usage")


def downgrade() -> None:
    # The columns come back empty: `tool_uses` at its `0024` server default, `usage` NULL. Nothing
    # reconstructs the rows, because the values are gone — `tool_calls` is a re-spelling rather than
    # a copy, and a turn's payload lives in `session_frames`, not in a shape this could invert.
    op.add_column(
        "session_messages",
        sa.Column(
            "tool_uses", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False
        ),
    )
    op.add_column("session_turns", sa.Column("usage", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
