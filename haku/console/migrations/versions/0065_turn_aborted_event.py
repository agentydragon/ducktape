"""An abort becomes an event: one more `session_events` kind, and the first authored one with a turn.

The operator stopping a turn was durable only as a `turn_id`-keyed `session_outbox` row — the one
non-reply artifact the console kept, kept in the channel's table rather than in the record.
`turn_aborted` puts it in the ordered stream beside the lease facts, and the room's "aborted" line
becomes a projection of that row.

**No column change.** `session_events.turn_id` is already nullable and
`ck_session_events_provenance_frames` already permits an authored row that names a turn — it
requires one only on the `frame_range` arm. So this is the CHECK on `kind` and nothing else.

**Additive, and safe for the length of a roll.** A widened CHECK forbids nothing the previous image
writes.

Whether that image could *read* one of these rows was argued here from the reader set as it stood,
and the argument expired: `x/subscription.ConversationStream.read` selects every row of a
conversation with no kind filter, and was written two days after this migration. The console
answers this at the column now — `session_events.kind` decodes a kind it has no words for instead of
raising — so a widened CHECK no longer depends on an inventory of readers holding still
(<../../README.md> § Vocabularies across a roll).

Revision ID: 0065
Revises: 0064
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0065"
down_revision: str | None = "0064"
branch_labels: str | None = None
depends_on: str | None = None

_TABLE = "session_events"
_KIND = "ck_session_events_kind"

# Spelled out rather than imported from the ORM, for the reason `0041` gives: a migration is a
# point-in-time statement about the database.
_CONVERSATION_KINDS = (
    "'message_completed','reasoning','tool_call_started','tool_call_completed','activity_started','activity_completed'"
)
_AUTHORED_KINDS = "'prompt_enqueued','session_adopted','lease_expired'"


def upgrade() -> None:
    op.drop_constraint(_KIND, _TABLE, type_="check")
    op.create_check_constraint(_KIND, _TABLE, f"kind IN ({_CONVERSATION_KINDS},{_AUTHORED_KINDS},'turn_aborted')")


def downgrade() -> None:
    op.execute(sa.text(f"DELETE FROM {_TABLE} WHERE kind = 'turn_aborted'"))
    op.drop_constraint(_KIND, _TABLE, type_="check")
    op.create_check_constraint(_KIND, _TABLE, f"kind IN ({_CONVERSATION_KINDS},{_AUTHORED_KINDS})")
