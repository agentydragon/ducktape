"""Record MCP auto-approval evaluation outcomes.

Revision ID: 0005
Revises: 0004
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("mcp_tool_calls", sa.Column("auto_approval_evaluation", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("mcp_tool_calls", "auto_approval_evaluation")
