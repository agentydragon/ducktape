"""Expand the conversation harness discriminator: add harness_kind beside runtime_kind.

C4d phase-2 of the #4772 vocabulary collapse (naming_and_layout.md §3.1): `runtime_kind` becomes
`harness_kind`. This is the **expand** release of a stored-column rename, run while previous API
replicas (post-#5050, which read and write `runtime_kind`) still serve, so both columns coexist:
the new image dual-writes both and keeps *reading* `runtime_kind`, and no replica reads a column
another has renamed away.

`harness_kind` is nullable here because a still-serving #5050 replica inserts conversations without
it during the roll. The read-switch release backfills those stragglers
(`UPDATE ... WHERE harness_kind IS NULL`) and adds NOT NULL; later releases stop writing/mapping
`runtime_kind` and then drop it. The conversation-droppable allowance (AGENTS.md) would permit
emptying the table instead of backfilling, but the backfill preserves the rows for free and is the
smaller change (as in 0108).

Revision ID: 0114
Revises: 0112
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0114"
down_revision: str | None = "0112"
branch_labels: str | None = None
depends_on: str | None = None

_CONSTRAINT = "ck_conversation_harness_kind"


def upgrade() -> None:
    op.add_column("conversation", sa.Column("harness_kind", sa.Text(), nullable=True))
    op.execute("UPDATE conversation SET harness_kind = runtime_kind")
    # NULL passes (a #5050 replica's expand-roll insert); the read-switch release adds NOT NULL.
    op.create_check_constraint(_CONSTRAINT, "conversation", "harness_kind IN ('claude_code', 'codex_app_server')")


def downgrade() -> None:
    op.drop_constraint(_CONSTRAINT, "conversation", type_="check")
    op.drop_column("conversation", "harness_kind")
