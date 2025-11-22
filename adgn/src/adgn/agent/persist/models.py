"""SQLAlchemy ORM models for agent persistence.

This module defines the database schema using SQLAlchemy 2.0 declarative models.
All models use native SQLAlchemy types with no custom serialization.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, Index, Integer, JSON, String, Text, func, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from adgn.agent.types import AgentID


# Define enums inline to avoid circular imports
# These match the enums in __init__.py
class PersistenceRunStatus(StrEnum):
    """Final run state stored in persistence layer."""
    RUNNING = "running"
    FINISHED = "finished"
    ERROR = "error"
    ABORTED = "aborted"


class EventType(StrEnum):
    USER_TEXT = "user_text"
    ASSISTANT_TEXT = "assistant_text"
    TOOL_CALL = "tool_call"
    FUNCTION_CALL_OUTPUT = "function_call_output"
    REASONING = "reasoning"
    RESPONSE = "response"


class PolicyStatus(StrEnum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    PROPOSED = "proposed"
    REJECTED = "rejected"


class ChatAuthor(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


class Base(DeclarativeBase):
    """Base class for all ORM models."""

    pass


class Agent(Base):
    """Agent configuration and metadata.

    An agent represents a configured AI assistant with MCP server connections
    and a preset configuration.
    """

    __tablename__ = "agents"

    id: Mapped[AgentID] = mapped_column(String, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    mcp_config: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)  # MCPConfig as JSON
    preset: Mapped[str] = mapped_column(String, nullable=False)  # Agent preset name

    # Relationships
    runs: Mapped[list["Run"]] = relationship(
        back_populates="agent", cascade="all, delete-orphan", passive_deletes=True
    )
    tool_calls: Mapped[list["ToolCall"]] = relationship(back_populates="agent", cascade="all, delete-orphan")
    policies: Mapped[list["Policy"]] = relationship(back_populates="agent", cascade="all, delete-orphan")
    chat_messages: Mapped[list["ChatMessage"]] = relationship(back_populates="agent", cascade="all, delete-orphan")
    chat_last_reads: Mapped[list["ChatLastRead"]] = relationship(
        back_populates="agent", cascade="all, delete-orphan"
    )


class Run(Base):
    """Execution session for an agent.

    Tracks a single conversation/execution session with start/end times,
    status, and associated events.
    """

    __tablename__ = "runs"

    id: Mapped[UUID] = mapped_column(String, primary_key=True)  # UUID stored as string
    agent_id: Mapped[AgentID | None] = mapped_column(ForeignKey("agents.id", ondelete="SET NULL"), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[PersistenceRunStatus] = mapped_column(String, nullable=False)
    system_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    model: Mapped[str | None] = mapped_column(String, nullable=True)
    model_params: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    # Relationships
    agent: Mapped["Agent | None"] = relationship(back_populates="runs")
    events: Mapped[list["Event"]] = relationship(back_populates="run", cascade="all, delete-orphan", passive_deletes=True)
    tool_calls: Mapped[list["ToolCall"]] = relationship(
        back_populates="run", cascade="all, delete-orphan", passive_deletes=True
    )

    __table_args__ = (Index("idx_runs_agent_started", "agent_id", "started_at"),)


class Event(Base):
    """Timestamped event within a run.

    Events represent individual interactions: user messages, assistant responses,
    tool calls, reasoning steps, etc. Each event has a typed payload stored as JSON.
    """

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)  # Sequence number within run
    event_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    type: Mapped[EventType] = mapped_column(String, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)  # Typed payload per EventType
    call_id: Mapped[str | None] = mapped_column(String, nullable=True)  # For tool call correlation
    tool_key: Mapped[str | None] = mapped_column(String, nullable=True)

    # Relationships
    run: Mapped["Run"] = relationship(back_populates="events")

    __table_args__ = (
        Index("idx_events_run_seq", "run_id", "seq", unique=True),
        Index("idx_events_call", "call_id"),
    )


class ToolCall(Base):
    """Tool call lifecycle tracking.

    Tracks tool calls through their lifecycle: pending → decided → executed.
    Stores the call details, approval decision, and execution result as JSON.
    """

    __tablename__ = "tool_calls"

    call_id: Mapped[str] = mapped_column(String, primary_key=True)
    run_id: Mapped[UUID | None] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), nullable=True)
    agent_id: Mapped[AgentID] = mapped_column(ForeignKey("agents.id", ondelete="CASCADE"), nullable=False)
    tool_call_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)  # ToolCall as JSON
    decision_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)  # Decision as JSON
    execution_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)  # ToolCallExecution as JSON
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Relationships
    run: Mapped["Run | None"] = relationship(back_populates="tool_calls")
    agent: Mapped["Agent"] = relationship(back_populates="tool_calls")

    __table_args__ = (
        Index("idx_tool_calls_run", "run_id"),
        Index("idx_tool_calls_agent", "agent_id"),
        Index("idx_tool_calls_decided", "decided_at"),
    )


class Policy(Base):
    """Per-agent approval policy with status tracking."""

    __tablename__ = "policies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    agent_id: Mapped[AgentID] = mapped_column(ForeignKey("agents.id", ondelete="CASCADE"), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[PolicyStatus] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Relationships
    agent: Mapped["Agent"] = relationship(back_populates="policies")

    __table_args__ = (
        Index("idx_policies_agent_status", "agent_id", "status"),
        Index("idx_policies_status", "status"),
        # Partial unique index: ensures only ONE ACTIVE policy per agent
        Index("idx_policies_agent_active", "agent_id", unique=True, sqlite_where=text("status = 'active'")),
    )


class ChatMessage(Base):
    """Chat message for an agent.

    Stores messages in the chat interface with author, MIME type, and content.
    """

    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    agent_id: Mapped[AgentID] = mapped_column(ForeignKey("agents.id", ondelete="CASCADE"), nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    author: Mapped[ChatAuthor] = mapped_column(String, nullable=False)
    mime: Mapped[str] = mapped_column(String, nullable=False)  # Keep as str (text/markdown, text/plain, etc.)
    content: Mapped[str] = mapped_column(Text, nullable=False)

    # Relationships
    agent: Mapped["Agent"] = relationship(back_populates="chat_messages")

    __table_args__ = (Index("idx_chat_messages_agent", "agent_id"),)


class ChatLastRead(Base):
    """Last read message position per agent and server.

    Tracks which message each server has read up to for an agent.
    """

    __tablename__ = "chat_last_read"

    agent_id: Mapped[AgentID] = mapped_column(ForeignKey("agents.id", ondelete="CASCADE"), primary_key=True)
    server_name: Mapped[str] = mapped_column(String, primary_key=True)
    last_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Relationships
    agent: Mapped["Agent"] = relationship(back_populates="chat_last_reads")
