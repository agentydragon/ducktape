"""Shared base classes and enums for Claude Code hook models."""

from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class PermissionMode(StrEnum):
    """Claude Code permission mode values."""

    DEFAULT = "default"
    PLAN = "plan"
    ACCEPT_EDITS = "acceptEdits"
    DONT_ASK = "dontAsk"
    AUTO = "auto"
    # CLEANUP(2026-03-30): bypassPermissions removed from v2.1.87 Zod schema
    # but kept for backwards compat with older versions.
    BYPASS_PERMISSIONS = "bypassPermissions"


class CamelModel(BaseModel):
    """Base for hook output models — serializes fields as camelCase, rejects extra fields."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")


class HookOutputBase(CamelModel):
    """Common fields for all hook outputs (matches Zod hookOutput schema)."""

    continue_: bool = Field(default=True, alias="continue")
    suppress_output: bool = False
    stop_reason: str | None = None
    decision: Literal["approve", "block"] | None = None
    reason: str | None = None
    system_message: str | None = None


class HookInputBase(BaseModel):
    """Common fields present in all hook inputs."""

    session_id: str
    transcript_path: Path
    cwd: Path
    permission_mode: PermissionMode | None = Field(
        default=None, description="Not sent by Claude Code Web for some SessionStart events"
    )
    agent_id: str | None = Field(
        default=None, description="Subagent identifier. Present only when the hook fires from within a subagent."
    )
    parent_session_id: str | None = Field(default=None, description="Parent session ID when running as a subagent.")
    agent_type: str | None = Field(
        default=None,
        description="Agent type name. Present in subagent context (with agent_id) or on main thread with --agent.",
    )
