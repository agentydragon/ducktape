"""A message's frame range cannot end where it never began.

`0045` added `session_messages`' inclusive frame range and checked only its *ordering*, so two
shapes stayed writable: a row with no range at all, and a row with a far end and no near end. This
closes the second, `NOT VALID`, and deliberately not the first: "every row carries a range" is not
expressible here, because NULL means two things on this table — a row whose frames are not yet
known, and the operator's own prompt, which crosses no wire and legitimately has none. The table
that can state it is `session_events`, whose provenance is `NOT NULL` and whose CHECK makes a frame
range and an authored row the only two possibilities.

A far end with no near end is nonsense in either direction that `frame_range | authored` union
allows: it is neither a range nor the absence of one. Every writer already satisfies it —
`begin_assistant` writes the near end at insert and `update_assistant` only ever widens from
there, and `set_message_source_frames` writes both ends in one statement — so adding it under a
`maxUnavailable: 0` roll cannot reject a write from the image still serving.

`NOT VALID` because the pre-#4105 rows are unpointed and this ships without first deciding whether
to recover or drop them; `VALIDATE CONSTRAINT` promotes it later without rewriting the table.

Revision ID: 0046
Revises: 0045
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0046"
down_revision: str | None = "0045"
branch_labels: str | None = None
depends_on: str | None = None

_CONSTRAINT = "ck_session_messages_source_anchored"

# Raw DDL rather than `op.create_check_constraint`, which has no way to say NOT VALID — and
# validating here would scan the unpointed history this deliberately tolerates.
_ADD = (
    f"ALTER TABLE session_messages ADD CONSTRAINT {_CONSTRAINT} "
    "CHECK (source_last_frame_seq IS NULL OR source_first_frame_seq IS NOT NULL) NOT VALID"
)


def upgrade() -> None:
    op.execute(sa.text(_ADD))


def downgrade() -> None:
    op.drop_constraint(_CONSTRAINT, "session_messages", type_="check")
