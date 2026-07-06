"""SQLAlchemy schema for haku-console's database."""

from __future__ import annotations

from sqlalchemy import BigInteger, Column, DateTime, ForeignKey, MetaData, Table, Text
from sqlalchemy.dialects.postgresql import ENUM, JSONB

metadata = MetaData()

TOOL_CALL_STATUS_VALUES = ("pending_approval", "running", "ok", "error", "denied")
TOOL_CALL_EVENT_TYPE_VALUES = ("tool_call_submitted", "approval_pending", "tool_call_updated")

tool_call_status_type = ENUM(*TOOL_CALL_STATUS_VALUES, name="tool_call_status", create_type=False)
tool_call_event_type = ENUM(*TOOL_CALL_EVENT_TYPE_VALUES, name="tool_call_event_type", create_type=False)

tool_calls = Table(
    "mcp_tool_calls",
    metadata,
    Column("tool_call_id", Text, primary_key=True),
    Column("server_id", Text, nullable=False),
    Column("server_title", Text, nullable=False),
    Column("tool_name", Text, nullable=False),
    Column("caller_principal", Text, nullable=False),
    Column("status", tool_call_status_type, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("arguments_json", JSONB, nullable=False),
    Column("rationale", Text, nullable=False),
    Column("request_title", Text, nullable=True),
    Column("client_request_id", Text, nullable=True),
    Column("state_request_id", Text, nullable=True),
    Column("request_digest", Text, nullable=False),
    Column("approval_id", Text, nullable=True),
    Column("decision_reason", Text, nullable=True),
    Column("result_json", JSONB, nullable=True),
    Column("error", Text, nullable=True),
)

tool_call_idempotency = Table(
    "mcp_tool_call_idempotency",
    metadata,
    Column("idempotency_key", Text, primary_key=True),
    Column("request_digest", Text, nullable=False),
    Column("tool_call_id", Text, ForeignKey("mcp_tool_calls.tool_call_id"), nullable=False),
)

tool_call_events = Table(
    "mcp_tool_call_events",
    metadata,
    Column("event_id", BigInteger, primary_key=True, autoincrement=True),
    Column("event_type", tool_call_event_type, nullable=False),
    Column("tool_call_id", Text, nullable=False),
    Column("status", tool_call_status_type, nullable=False),
    Column("approval_id", Text, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
)


def sqlalchemy_url(database_url: str) -> str:
    return database_url.replace("postgresql://", "postgresql+psycopg://", 1)
