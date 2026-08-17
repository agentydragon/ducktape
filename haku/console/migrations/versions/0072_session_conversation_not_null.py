"""A session belongs to a conversation, and the schema can finally say so.

`0064` added `sessions.conversation_id` nullable for exactly one reason: the image running while
that migration applied does not name the column in its `INSERT INTO sessions`, so a `NOT NULL`
would have rejected the first session of the roll. It backfilled every row that predated it — one
conversation per session, Matrix sessions grouped by room — and every writer since names the
column, so the nullability has been describing a window rather than a state.

That window is closed: every `haku-console` pod runs an image that fills the column, which is the
gate `0064` named and the same gate that frees a reader to key on it. The column is now what it
always meant.

Plain `SET NOT NULL` rather than the `NOT VALID` check-then-validate dance `0046`/`0058` use on
`session_messages`: that pattern buys a short lock on a table too large to scan under one, and
`sessions` was purged on 2026-08-16 (`0058`) and holds only what the releases since then created.

Revision ID: 0072
Revises: 0071
"""

from __future__ import annotations

from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0072"
down_revision: str | None = "0071"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.alter_column("sessions", "conversation_id", existing_type=postgresql.UUID(as_uuid=True), nullable=False)


def downgrade() -> None:
    op.alter_column("sessions", "conversation_id", existing_type=postgresql.UUID(as_uuid=True), nullable=True)
