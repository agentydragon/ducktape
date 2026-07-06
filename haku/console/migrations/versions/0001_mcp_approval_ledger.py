"""Create MCP approval ledger.

Revision ID: 0001
Revises: None
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ENUM, JSONB

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TOOL_CALL_STATUS_VALUES = ("pending_approval", "running", "ok", "error", "denied")
TOOL_CALL_EVENT_TYPE_VALUES = ("tool_call_submitted", "approval_pending", "tool_call_updated")


def _tool_call_status_enum(*, create_type: bool = False) -> ENUM:
    return ENUM(*TOOL_CALL_STATUS_VALUES, name="tool_call_status", create_type=create_type)


def _tool_call_event_type_enum(*, create_type: bool = False) -> ENUM:
    return ENUM(*TOOL_CALL_EVENT_TYPE_VALUES, name="tool_call_event_type", create_type=create_type)


def upgrade() -> None:
    bind = op.get_bind()
    _tool_call_status_enum(create_type=True).create(bind, checkfirst=True)
    _tool_call_event_type_enum(create_type=True).create(bind, checkfirst=True)

    op.create_table(
        "mcp_tool_calls",
        sa.Column("tool_call_id", sa.Text(), primary_key=True),
        sa.Column("server_id", sa.Text(), nullable=False),
        sa.Column("server_title", sa.Text(), nullable=False),
        sa.Column("tool_name", sa.Text(), nullable=False),
        sa.Column("caller_principal", sa.Text(), nullable=False),
        sa.Column("status", _tool_call_status_enum(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("arguments_json", JSONB(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("client_request_id", sa.Text(), nullable=True),
        sa.Column("state_request_id", sa.Text(), nullable=True),
        sa.Column("request_digest", sa.Text(), nullable=False),
        sa.Column("decision_reason", sa.Text(), nullable=True),
        sa.Column("result_json", JSONB(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
    )
    op.create_index("idx_mcp_tool_calls_created_at", "mcp_tool_calls", ["created_at"])
    op.create_index(
        "uq_mcp_tool_calls_caller_client_request",
        "mcp_tool_calls",
        ["caller_principal", "client_request_id"],
        unique=True,
        postgresql_where=sa.text("client_request_id IS NOT NULL"),
    )

    op.create_table(
        "mcp_tool_call_events",
        sa.Column("event_id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("event_type", _tool_call_event_type_enum(), nullable=False),
        sa.Column("tool_call_id", sa.Text(), nullable=False),
        sa.Column("status", _tool_call_status_enum(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "idx_mcp_tool_call_events_tool_call_id_event_id", "mcp_tool_call_events", ["tool_call_id", "event_id"]
    )


def downgrade() -> None:
    op.drop_index("idx_mcp_tool_call_events_tool_call_id_event_id", table_name="mcp_tool_call_events")
    op.drop_table("mcp_tool_call_events")
    op.drop_index("uq_mcp_tool_calls_caller_client_request", table_name="mcp_tool_calls")
    op.drop_index("idx_mcp_tool_calls_created_at", table_name="mcp_tool_calls")
    op.drop_table("mcp_tool_calls")
    bind = op.get_bind()
    _tool_call_event_type_enum(create_type=True).drop(bind, checkfirst=True)
    _tool_call_status_enum(create_type=True).drop(bind, checkfirst=True)
