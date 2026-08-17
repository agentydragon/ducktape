"""Two more `session_events` kinds: a prompt that was refused, and input that could not be read.

Both facts were announced into the room from the stack frame that noticed them and kept nowhere
(<../../debug/channel_write_audit.md> rows 11 and 12), so a crash between acknowledging a message to
the homeserver and posting the notice told the operator nothing and left no record either. They
are `authored` for the reason every console-witnessed fact is: no frame carries them, and none
ever will.

**Additive, and safe for the length of a roll** (<../../README.md> § Perimeter / deploy). A
widened CHECK forbids nothing the previous image writes, and that image never *reads* one of these
rows: its two queries against this table filter to the tool-call kinds in SQL and select by
`turn_id`, and neither of these names a turn. Both matter, because `TextBackedStrEnumUnionColumn`
parses the column — a row of an unknown kind reaching either would raise rather than degrade.

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
# point-in-time statement about the database and must not change meaning when another file is
# edited.
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
