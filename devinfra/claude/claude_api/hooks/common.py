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
