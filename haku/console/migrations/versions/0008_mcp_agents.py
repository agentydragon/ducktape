"""Add operator-authored display names for OAuth MCP agents.

Revision ID: 0008
Revises: 0007
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Existing calls predate required caller display names. Do not invent historical identity
    # labels: clear the ledger before making the new field mandatory, as in migration 0007.
    op.execute("DELETE FROM mcp_tool_call_events")
    op.execute("DELETE FROM mcp_tool_calls")
    op.add_column("mcp_tool_calls", sa.Column("caller_display_name", sa.Text(), nullable=False))

    op.create_table(
        "mcp_agents",
        sa.Column(
            "agent_id",
            sa.Text(),
            primary_key=True,
            comment="The OAuth agent's stable Dynamic Client Registration client_id.",
        ),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("display_name", name="uq_mcp_agents_display_name"),
        sa.CheckConstraint("length(trim(display_name)) > 0", name="ck_mcp_agents_display_name_not_empty"),
    )


def downgrade() -> None:
    op.drop_table("mcp_agents")
    op.drop_column("mcp_tool_calls", "caller_display_name")
