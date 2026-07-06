"""SQLAlchemy ORM schema for haku-console's database."""

from __future__ import annotations

import datetime
from collections.abc import Mapping
from typing import Any

from sqlalchemy import BigInteger, DateTime, Index, Text
from sqlalchemy.dialects.postgresql import ENUM, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

TOOL_CALL_STATUS_VALUES = ("pending_approval", "running", "ok", "error", "denied")
TOOL_CALL_EVENT_TYPE_VALUES = ("tool_call_submitted", "approval_pending", "tool_call_updated")


class Base(DeclarativeBase):
    pass


tool_call_status_type = ENUM(*TOOL_CALL_STATUS_VALUES, name="tool_call_status", create_type=False)
tool_call_event_type = ENUM(*TOOL_CALL_EVENT_TYPE_VALUES, name="tool_call_event_type", create_type=False)


class McpToolCall(Base):
    __tablename__ = "mcp_tool_calls"
    __table_args__ = (Index("idx_mcp_tool_calls_created_at", "created_at"),)

    tool_call_id: Mapped[str] = mapped_column(Text, primary_key=True)
    server_id: Mapped[str] = mapped_column(Text, nullable=False)
    tool_name: Mapped[str] = mapped_column(Text, nullable=False)
    caller_principal: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(tool_call_status_type, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    arguments_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    @classmethod
    def from_record_data(cls, values: Mapping[str, Any]) -> McpToolCall:
        status = values["status"]
        return cls(
            tool_call_id=values["tool_call_id"],
            server_id=values["server_id"],
            tool_name=values["tool_name"],
            caller_principal=values["caller_principal"],
            status=status.value if hasattr(status, "value") else status,
            created_at=values["created_at"],
            updated_at=values["updated_at"],
            arguments_json=values["arguments"],
            rationale=values["rationale"],
            title=values["title"],
            result_json=values.get("result"),
            error=values.get("error"),
        )

    def to_record_data(self) -> dict[str, Any]:
        return {
            "tool_call_id": self.tool_call_id,
            "server_id": self.server_id,
            "tool_name": self.tool_name,
            "caller_principal": self.caller_principal,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "arguments": self.arguments_json,
            "rationale": self.rationale,
            "title": self.title,
            "result": self.result_json,
            "error": self.error,
        }


class McpToolCallEvent(Base):
    __tablename__ = "mcp_tool_call_events"
    __table_args__ = (Index("idx_mcp_tool_call_events_tool_call_id_event_id", "tool_call_id", "event_id"),)

    event_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event_type: Mapped[str] = mapped_column(tool_call_event_type, nullable=False)
    tool_call_id: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(tool_call_status_type, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    def to_event_data(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "tool_call_id": self.tool_call_id,
            "status": self.status,
            "created_at": self.created_at,
        }


metadata = Base.metadata


def sqlalchemy_url(database_url: str) -> str:
    return database_url.replace("postgresql://", "postgresql+psycopg://", 1)
