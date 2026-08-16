"""Require a projected message to say which frames produced it.

`0045` added the inclusive range and checked only its *ordering*, so a row with no range at all
stayed writable — which is the case the constraint exists to prevent
(<../../../plans/chat_runtime_projection.md> § "The projection is not a one-way door": "New and
updated rows must carry a range; existing rows are tolerated and unchecked").

Two rules, both `NOT VALID`, so existing rows are tolerated and new writes are not:

- An **assistant** row is a projection of the frame log, so it must name where it began.
- A **user** row is authored: the operator's prompt exists before the frame it goes out as, and a
  prompt no turn ever claims never acquires one. A null range stays legal there. This is the
  plan's `frame_range | authored` union carried by `role` rather than by a discriminator column —
  `session_messages` has exactly two writers and they are exactly those two kinds.
- A far end with no near end is a range in neither.

**Roll safety.** The image currently serving already satisfies both rules, which is why adding
them under `maxUnavailable: 0` does not reject an old replica's writes: since #4105 every
`begin_assistant` call site passes `source_first_frame_seq`, and `update_assistant` only ever
widens a range whose near end that insert wrote.

**Expand half only.** `VALIDATE CONSTRAINT` needs the pre-#4105 assistant rows backfilled first,
which the plan gives to the reprojection tool and bounds by stage 1's frame completeness.

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

# Written as raw DDL rather than through `op.create_check_constraint`, which has no way to say
# NOT VALID — and validating here would scan (and reject) the unpointed history this migration
# deliberately tolerates.
_CONSTRAINTS = {
    "ck_session_messages_projected_source": "role <> 'assistant' OR source_first_frame_seq IS NOT NULL",
    "ck_session_messages_source_anchored": "source_last_frame_seq IS NULL OR source_first_frame_seq IS NOT NULL",
}


def upgrade() -> None:
    for name, expression in _CONSTRAINTS.items():
        op.execute(sa.text(f"ALTER TABLE session_messages ADD CONSTRAINT {name} CHECK ({expression}) NOT VALID"))


def downgrade() -> None:
    for name in _CONSTRAINTS:
        op.drop_constraint(name, "session_messages", type_="check")
