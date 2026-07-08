"""Add denial_reason to mcp_tool_calls.

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-08
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import Column, Text

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("mcp_tool_calls", Column("denial_reason", Text, nullable=True))


def downgrade() -> None:
    op.drop_column("mcp_tool_calls", "denial_reason")
