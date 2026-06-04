"""Drop agent_runs.container_stdout / container_stderr columns.

Container logs now live in Loki and are served by GET /api/runs/{id}/logs.
The DB columns were a redundant second copy written by the orchestration layer;
remove them.

Revision ID: 20260604000001
Revises: 20260604000000
Create Date: 2026-06-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260604000001"
down_revision: str | None = "20260604000000"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_column("agent_runs", "container_stdout")
    op.drop_column("agent_runs", "container_stderr")


def downgrade() -> None:
    op.add_column("agent_runs", sa.Column("container_stdout", sa.Text(), nullable=True))
    op.add_column("agent_runs", sa.Column("container_stderr", sa.Text(), nullable=True))
