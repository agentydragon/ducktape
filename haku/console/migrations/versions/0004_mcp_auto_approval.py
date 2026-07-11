"""Record MCP auto-approval policy provenance.

Revision ID: 0004
Revises: 0003
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("mcp_tool_calls", sa.Column("approval_policy_id", sa.Text(), nullable=True))
    op.add_column("mcp_tool_calls", sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("mcp_tool_calls", "approved_at")
    op.drop_column("mcp_tool_calls", "approval_policy_id")
