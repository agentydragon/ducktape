"""A lease records which replica holds it, so a dead session names the pod to go read.

Revision ID: 0031
Revises: 0030
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0031"
down_revision: str | None = "0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Additive and staying that way. Nullable is not a concession to the roll — a replica on the
    # previous image renews without it and those rows read as unheld, which is correct — it is the
    # column's second meaning: NULL is the creator's provisioning grant, where no replica holds
    # the session yet. A NOT NULL here could only be satisfied by inventing a holder.
    op.add_column("claude_chat_sessions", sa.Column("lease_holder", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("claude_chat_sessions", "lease_holder")
