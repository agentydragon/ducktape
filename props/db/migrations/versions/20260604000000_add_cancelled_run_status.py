"""Add 'cancelled' value to agent_run_status_enum.

Records agent runs whose host task was cancelled before the container finished
(e.g. the GraderSupervisor replacing a snapshot grader during reconcile, or
shutdown) as a terminal status, instead of leaking as 'in_progress' forever.

Revision ID: 20260604000000
Revises: 20260228000000
Create Date: 2026-06-04
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260604000000"
down_revision: str | None = "20260228000000"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # PostgreSQL 12+ allows ALTER TYPE ... ADD VALUE inside a transaction as long
    # as the new value is not used in the same transaction (this migration only
    # adds it). IF NOT EXISTS keeps it idempotent.
    op.execute("ALTER TYPE agent_run_status_enum ADD VALUE IF NOT EXISTS 'cancelled'")


def downgrade() -> None:
    # PostgreSQL has no DROP VALUE for enums (removal requires recreating the type
    # and rewriting every dependent column). Leaving 'cancelled' in place on
    # downgrade is harmless.
    pass
