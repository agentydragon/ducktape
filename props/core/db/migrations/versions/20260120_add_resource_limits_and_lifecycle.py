"""Add resource limits, lifecycle columns to agent_runs; drop completion_summary.

Revision ID: 20260120_add_resource_limits_and_lifecycle
Revises: 20260120_add_container_exit_code
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import TIMESTAMP

revision = "20260120_add_resource_limits_and_lifecycle"
down_revision = "20260120_add_container_exit_code"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add resource limit columns
    op.add_column(
        "agent_runs",
        sa.Column(
            "budget_tokens",
            sa.Integer(),
            nullable=True,
            comment="Max tokens allowed for this agent (including child agents). Enforced by proxy.",
        ),
    )
    op.add_column(
        "agent_runs",
        sa.Column(
            "timeout_seconds",
            sa.Integer(),
            nullable=True,
            comment="Max seconds before agent is killed. Enforced by agent_registry.",
        ),
    )

    # Add lifecycle timestamp columns
    op.add_column(
        "agent_runs",
        sa.Column(
            "started_at",
            TIMESTAMP(timezone=True),
            nullable=True,
            comment="When container started executing",
        ),
    )
    op.add_column(
        "agent_runs",
        sa.Column(
            "ended_at",
            TIMESTAMP(timezone=True),
            nullable=True,
            comment="When container finished (success or failure)",
        ),
    )

    # Drop completion_summary column (no longer used)
    op.drop_column("agent_runs", "completion_summary")


def downgrade() -> None:
    # Restore completion_summary column
    op.add_column(
        "agent_runs",
        sa.Column(
            "completion_summary",
            sa.Text(),
            nullable=True,
        ),
    )

    # Drop lifecycle columns
    op.drop_column("agent_runs", "ended_at")
    op.drop_column("agent_runs", "started_at")

    # Drop resource limit columns
    op.drop_column("agent_runs", "timeout_seconds")
    op.drop_column("agent_runs", "budget_tokens")
