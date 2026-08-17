"""Which prompt carries an inbound Matrix event, so a re-delivered one is not asked twice.

The sync loop advances its watermark after the prompt commits, so a crash between the two
re-delivers a batch the session already holds. The row here is written in the prompt's own
transaction, which is what lets ingress tell "already carried" from "not yet offered" — and what
lets it find a prompt whose session died before answering, and offer that message again.

**Additive, and safe for the length of a roll** (<../../README.md> § Perimeter / deploy). A new
table nothing else references: the previous image neither writes it nor joins through it, and
messages it accepts while the roll is in flight simply have no row — they are offered as new, which
is the behaviour that image already has.

Revision ID: 0074
Revises: 0072
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0074"
down_revision: str | None = "0072"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "matrix_ingress_event",
        sa.Column("event_id", sa.Text(), primary_key=True),
        sa.Column(
            "message_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("session_messages.message_id", ondelete="CASCADE"),
            nullable=False,
        ),
    )
    op.create_index("idx_matrix_ingress_event_message", "matrix_ingress_event", ["message_id"])


def downgrade() -> None:
    op.drop_index("idx_matrix_ingress_event_message", table_name="matrix_ingress_event")
    op.drop_table("matrix_ingress_event")
