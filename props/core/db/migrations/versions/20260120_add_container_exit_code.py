"""Add container_exit_code column to agent_runs.

Revision ID: 20260120_add_container_exit_code
Revises: 20260119_notify_critique_changes
"""

from alembic import op
import sqlalchemy as sa

revision = "20260120_add_container_exit_code"
down_revision = "20260119_notify_critique_changes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agent_runs",
        sa.Column(
            "container_exit_code",
            sa.Integer(),
            nullable=True,
            comment="Container exit code (NULL if still running or not container-based)",
        ),
    )


def downgrade() -> None:
    op.drop_column("agent_runs", "container_exit_code")
