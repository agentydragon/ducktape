"""Two more `session_events` kinds: a prompt that was refused, and input that could not be read.

Both facts were announced into the room from the stack frame that noticed them and kept nowhere, so
a crash between acknowledging a message to the homeserver and posting the notice told the operator
nothing and left no record either. They are `authored` for the reason every console-witnessed fact
is: no frame carries them, and none ever will.

**Additive, and safe for the length of a roll.** A widened CHECK forbids nothing the previous image
writes.

This carried the same reader-set argument `0065` did, and it expired the same way — see that
migration. The column tolerates a kind it has no words for now, so the claim is no longer one a
later reader can falsify by existing (<../../README.md> § Vocabularies across a roll).

Revision ID: 0066
Revises: 0065
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0066"
down_revision: str | None = "0065"
branch_labels: str | None = None
depends_on: str | None = None

_TABLE = "session_events"
_KIND = "ck_session_events_kind"

# Spelled out rather than imported from the ORM, for the reason `0041` gives: a migration is a
# point-in-time statement about the database.
_ESTABLISHED = (
    "'message_completed','reasoning','tool_call_started','tool_call_completed',"
    "'activity_started','activity_completed','prompt_enqueued','session_adopted','lease_expired',"
    "'turn_aborted'"
)
_ADDED = "'prompt_rejected','unreadable_input'"


def upgrade() -> None:
    op.drop_constraint(_KIND, _TABLE, type_="check")
    op.create_check_constraint(_KIND, _TABLE, f"kind IN ({_ESTABLISHED},{_ADDED})")


def downgrade() -> None:
    op.execute(sa.text(f"DELETE FROM {_TABLE} WHERE kind IN ({_ADDED})"))
    op.drop_constraint(_KIND, _TABLE, type_="check")
    op.create_check_constraint(_KIND, _TABLE, f"kind IN ({_ESTABLISHED})")
