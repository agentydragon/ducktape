"""Add container log columns to agent_runs.

Revision ID: 20260118_add_container_logs
Revises: 20260118_add_llm_requests
Create Date: 2026-01-18

For in-container agent loops, we capture stdout/stderr from the container
after it exits and store them in the agent_runs table for debugging and
observability.
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20260118_add_container_logs"
down_revision = "20260118_add_llm_requests"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add container log columns
    op.add_column("agent_runs", sa.Column("container_stdout", sa.Text(), nullable=True))
    op.add_column("agent_runs", sa.Column("container_stderr", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("agent_runs", "container_stderr")
    op.drop_column("agent_runs", "container_stdout")
