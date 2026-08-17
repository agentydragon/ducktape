"""Let a message row say why it has no frame range.

`0045` filled `source_{first,last}_frame_seq` for the rows whose `agent_message_id` named an
`assistant` frame; the rest stayed NULL, and NULL there means both "nobody has looked" and
"looked and could not tell" (`0046`'s docstring, and the column's own comment). This adds the
column that separates them, so the recovery's outcome is stored beside each row rather than
living in whatever report the operator last ran.

**No rows are written here, deliberately.** Recovering a range means re-projecting a session's
frames through the Python fold, which is neither a SQL statement nor bounded by anything this table
knows. Startup applies migrations before serving and the Deployment rolls at `maxUnavailable: 0`
(console README § Perimeter / deploy), so a fold over the frame log inside `upgrade()` would hold
every replacement replica out of Ready for its duration. The scan was an operator-invoked path
instead, and this migration gave it somewhere to record what it could not do.

Additive and safe for the length of a roll: a replica on the previous image neither selects nor
writes this column, and both constraints are satisfied by every existing row — `unpointable_reason`
is NULL on all of them — so nothing an old replica can write is rejected.

Revision ID: 0055
Revises: 0054
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0055"
down_revision: str | None = "0054"
branch_labels: str | None = None
depends_on: str | None = None

_TABLE = "session_messages"
_VALUE = "ck_session_messages_unpointable_reason"
_EXCLUSIVE = "ck_session_messages_unpointable_exclusive"


def upgrade() -> None:
    op.add_column(_TABLE, sa.Column("unpointable_reason", sa.Text(), nullable=True))
    op.create_check_constraint(
        _VALUE,
        _TABLE,
        "unpointable_reason IS NULL "
        "OR unpointable_reason IN ('no_matching_projection','ambiguous_text','out_of_order')",
    )
    # A reason is why there is no range; a row carrying both says two contradictory things.
    op.create_check_constraint(_EXCLUSIVE, _TABLE, "unpointable_reason IS NULL OR source_first_frame_seq IS NULL")


def downgrade() -> None:
    op.drop_constraint(_EXCLUSIVE, _TABLE, type_="check")
    op.drop_constraint(_VALUE, _TABLE, type_="check")
    op.drop_column(_TABLE, "unpointable_reason")
