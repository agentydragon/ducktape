"""SQLAlchemy ORM schema for haku-console's database."""

from __future__ import annotations

import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, Index, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from haku.console.tool_calls import ToolCallEvent, ToolCallEventType, ToolCallRecord, ToolCallStatus
from util.sqlalchemy_types import StrEnumColumn


class Base(DeclarativeBase):
    pass


class McpToolCall(Base):
    __tablename__ = "mcp_tool_calls"
    __table_args__ = (Index("idx_mcp_tool_calls_created_at", "created_at"),)

    tool_call_id: Mapped[str] = mapped_column(Text, primary_key=True)
    server_id: Mapped[str] = mapped_column(Text, nullable=False)
    tool_name: Mapped[str] = mapped_column(Text, nullable=False)
    caller_principal: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[ToolCallStatus] = mapped_column(
        StrEnumColumn(ToolCallStatus, name="tool_call_status"), nullable=False
    )
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    arguments_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    denial_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    approval_policy_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    auto_approval_evaluation: Mapped[str | None] = mapped_column(Text, nullable=True)
    approved_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    @classmethod
    def from_record(cls, record: ToolCallRecord) -> McpToolCall:
        return cls(
            tool_call_id=record.tool_call_id,
            server_id=record.server_id,
            tool_name=record.tool_name,
            caller_principal=record.caller_principal,
            status=record.status,
            created_at=record.created_at,
            updated_at=record.updated_at,
            arguments_json=record.arguments,
            rationale=record.rationale,
            title=record.title,
            result_json=record.result,
            error=record.error,
            denial_reason=record.denial_reason,
            approval_policy_id=record.approval_policy_id,
            auto_approval_evaluation=record.auto_approval_evaluation,
            approved_at=record.approved_at,
        )

    def to_record(self) -> ToolCallRecord:
        return ToolCallRecord(
            tool_call_id=self.tool_call_id,
            server_id=self.server_id,
            tool_name=self.tool_name,
            caller_principal=self.caller_principal,
            status=self.status,
            created_at=self.created_at,
            updated_at=self.updated_at,
            arguments=self.arguments_json,
            rationale=self.rationale,
            title=self.title,
            result=self.result_json,
            error=self.error,
            denial_reason=self.denial_reason,
            approval_policy_id=self.approval_policy_id,
            auto_approval_evaluation=self.auto_approval_evaluation,
            approved_at=self.approved_at,
        )


class McpToolCallEvent(Base):
    __tablename__ = "mcp_tool_call_events"
    __table_args__ = (Index("idx_mcp_tool_call_events_tool_call_id_event_id", "tool_call_id", "event_id"),)

    event_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event_type: Mapped[ToolCallEventType] = mapped_column(
        StrEnumColumn(ToolCallEventType, name="tool_call_event_type"), nullable=False
    )
    tool_call_id: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[ToolCallStatus] = mapped_column(
        StrEnumColumn(ToolCallStatus, name="tool_call_status"), nullable=False
    )
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    def to_event(self) -> ToolCallEvent:
        return ToolCallEvent(
            event_id=self.event_id,
            event_type=self.event_type,
            tool_call_id=self.tool_call_id,
            status=self.status,
            created_at=self.created_at,
        )


class McpOperatorOAuthAssociation(Base):
    __tablename__ = "mcp_operator_oauth_associations"
    __table_args__ = (Index("idx_mcp_operator_oauth_associations_operator", "operator_principal"),)

    server_id: Mapped[str] = mapped_column(Text, primary_key=True)
    operator_principal: Mapped[str] = mapped_column(Text, primary_key=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    client_id: Mapped[str] = mapped_column(Text, nullable=False)
    client_secret: Mapped[str | None] = mapped_column(Text, nullable=True)
    client_secret_expires_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    token_endpoint_auth_method: Mapped[str | None] = mapped_column(Text, nullable=True)
    token_endpoint: Mapped[str] = mapped_column(Text, nullable=False)
    resource: Mapped[str | None] = mapped_column(Text, nullable=True)
    access_token: Mapped[str] = mapped_column(Text, nullable=False)
    refresh_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    token_type: Mapped[str] = mapped_column(Text, nullable=False)
    scope: Mapped[str | None] = mapped_column(Text, nullable=True)
    token_expires_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class McpOperatorOAuthFlow(Base):
    __tablename__ = "mcp_operator_oauth_flows"
    __table_args__ = (
        Index("idx_mcp_operator_oauth_flows_server_operator", "server_id", "operator_principal"),
        Index("idx_mcp_operator_oauth_flows_expires_at", "expires_at"),
    )

    state: Mapped[str] = mapped_column(Text, primary_key=True)
    server_id: Mapped[str] = mapped_column(Text, nullable=False)
    operator_principal: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    redirect_uri: Mapped[str] = mapped_column(Text, nullable=False)
    code_verifier: Mapped[str] = mapped_column(Text, nullable=False)
    client_id: Mapped[str] = mapped_column(Text, nullable=False)
    client_secret: Mapped[str | None] = mapped_column(Text, nullable=True)
    client_secret_expires_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    token_endpoint_auth_method: Mapped[str | None] = mapped_column(Text, nullable=True)
    token_endpoint: Mapped[str] = mapped_column(Text, nullable=False)
    resource: Mapped[str | None] = mapped_column(Text, nullable=True)
    scope: Mapped[str | None] = mapped_column(Text, nullable=True)


metadata = Base.metadata
