"""Create MCP approval ledger.

Revision ID: 0001
Revises: None
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "mcp_tool_calls",
        sa.Column("tool_call_id", sa.Text(), primary_key=True),
        sa.Column("server_id", sa.Text(), nullable=False),
        sa.Column("server_title", sa.Text(), nullable=False),
        sa.Column("tool_name", sa.Text(), nullable=False),
        sa.Column("caller_principal", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("arguments_json", JSONB(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("request_title", sa.Text(), nullable=True),
        sa.Column("client_request_id", sa.Text(), nullable=True),
        sa.Column("state_request_id", sa.Text(), nullable=True),
        sa.Column("request_digest", sa.Text(), nullable=False),
        sa.Column("approval_id", sa.Text(), nullable=True),
        sa.Column("decision_reason", sa.Text(), nullable=True),
        sa.Column("result_json", JSONB(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "status IN ('approval_required', 'running', 'ok', 'error', 'denied')", name="mcp_tool_calls_status_check"
        ),
    )
    op.create_index("idx_mcp_tool_calls_created_at", "mcp_tool_calls", ["created_at"])
    op.create_index(
        "idx_mcp_tool_calls_approval_id",
        "mcp_tool_calls",
        ["approval_id"],
        postgresql_where=sa.text("approval_id IS NOT NULL"),
    )

    op.create_table(
        "mcp_tool_call_idempotency",
        sa.Column("idempotency_key", sa.Text(), primary_key=True),
        sa.Column("request_digest", sa.Text(), nullable=False),
        sa.Column("tool_call_id", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["tool_call_id"], ["mcp_tool_calls.tool_call_id"]),
    )

    op.create_table(
        "mcp_tool_call_events",
        sa.Column("event_id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("tool_call_id", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("approval_id", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "event_type IN ('tool_call_submitted', 'approval_pending', 'tool_call_updated')",
            name="mcp_tool_call_events_event_type_check",
        ),
    )
    op.create_index(
        "idx_mcp_tool_call_events_tool_call_id_event_id", "mcp_tool_call_events", ["tool_call_id", "event_id"]
    )


def downgrade() -> None:
    op.drop_index("idx_mcp_tool_call_events_tool_call_id_event_id", table_name="mcp_tool_call_events")
    op.drop_table("mcp_tool_call_events")
    op.drop_table("mcp_tool_call_idempotency")
    op.drop_index("idx_mcp_tool_calls_approval_id", table_name="mcp_tool_calls")
    op.drop_index("idx_mcp_tool_calls_created_at", table_name="mcp_tool_calls")
    op.drop_table("mcp_tool_calls")
