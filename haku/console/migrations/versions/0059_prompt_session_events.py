"""Let the ordered stream carry the operator's question: one more `session_events` kind.

`enqueue_prompt` wrote a `session_messages` row and no event, so `session_events` held the agent's
half of a conversation and not the operator's, and `event_seq` addressed only that half
(<../../plans/session_channels.md> § 4). The prompt is `authored` for the same reason a lease
change is — it has crossed no wire when it is accepted, and `next_prompt` hands it to the CLI
later. It reads as conversation rather than as a fact about the session, but membership is decided
by whether a frame carried the row, so its kind is an `AuthoredEventKind` (`chat_models`).

**Additive, and safe for the length of a roll** (<../../README.md> § Perimeter / deploy).
A widened CHECK forbids nothing the previous image writes, and that image never *reads* one of
these rows: the two queries it makes against this table are the transcript's tool-call view, which
filters to `tool_call_started`/`tool_call_completed` in SQL, and `reprojection`'s per-turn read,
which selects by `turn_id` — and a prompt row names no turn. Both matter, because
`TextBackedStrEnumUnionColumn` parses the column: a row of an unknown kind reaching either would
raise rather than degrade.

Revision ID: 0059
Revises: 0058
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0059"
down_revision: str | None = "0058"
branch_labels: str | None = None
depends_on: str | None = None

_TABLE = "session_events"
_KIND = "ck_session_events_kind"

# Spelled out rather than imported from the ORM, for the reason `0041` gives: a migration is a
# point-in-time statement about the database and must not change meaning when another file is
# edited.
_CONVERSATION_KINDS = (
    "'message_completed','reasoning','tool_call_started','tool_call_completed','activity_started','activity_completed'"
)
_AUTHORED_KINDS = "'session_adopted','lease_expired'"


def upgrade() -> None:
    op.drop_constraint(_KIND, _TABLE, type_="check")
    op.create_check_constraint(_KIND, _TABLE, f"kind IN ({_CONVERSATION_KINDS},{_AUTHORED_KINDS},'prompt_enqueued')")


def downgrade() -> None:
    op.execute(sa.text(f"DELETE FROM {_TABLE} WHERE kind = 'prompt_enqueued'"))
    op.drop_constraint(_KIND, _TABLE, type_="check")
    op.create_check_constraint(_KIND, _TABLE, f"kind IN ({_CONVERSATION_KINDS},{_AUTHORED_KINDS})")
