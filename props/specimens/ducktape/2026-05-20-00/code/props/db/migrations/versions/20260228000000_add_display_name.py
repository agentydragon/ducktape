"""Add display_name column to agent_definitions.

Stores the human-readable display name extracted from the OCI image label
org.opencontainers.image.title at manifest push time.

Revision ID: 20260228000000
Revises: 20260227000000
Create Date: 2026-02-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260228000000"
down_revision: str | None = "20260227000000"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agent_definitions",
        sa.Column(
            "display_name",
            sa.Text(),
            nullable=True,
            comment="Human-readable name from OCI label org.opencontainers.image.title",
        ),
    )


def downgrade() -> None:
    op.drop_column("agent_definitions", "display_name")
