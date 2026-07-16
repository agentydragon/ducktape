"""Remove durable tool-call transition events.

Revision ID: 0010
Revises: 0009

Tool-call rows are the durable, actor-scoped source of truth. PostgreSQL notifications carry only
lossy invalidations, so the former cursor/replay table and its event-type enum are unnecessary.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from sqlalchemy.dialects.postgresql import ENUM

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_table("mcp_tool_call_events")
    ENUM(name="tool_call_event_type").drop(op.get_bind(), checkfirst=True)


def downgrade() -> None:
    raise RuntimeError("0010 intentionally removes redundant transition history")
