"""Pydantic models for Claude Code SessionStart hook (non-REPL)."""

from enum import StrEnum
from typing import Literal

from pydantic import Field

from devinfra.claude.claude_api.hooks.common import CamelModel, HookInputBase


class HookSource(StrEnum):
    """Source of the SessionStart hook event."""

    STARTUP = "startup"
    RESUME = "resume"
    CLEAR = "clear"
    COMPACT = "compact"


class SessionStartHookInput(HookInputBase):
    """Input for Claude Code SessionStart hooks (parsed from stdin JSON)."""

    model: str | None = Field(default=None, description="Not always sent by Claude Code")
    hook_event_name: Literal["SessionStart"] = "SessionStart"
    source: HookSource


class SessionStartHookSpecificOutput(CamelModel):
    hook_event_name: Literal["SessionStart"] = "SessionStart"
    additional_context: str | None = Field(default=None, description="Context added to Claude's system prompt")
    initial_user_message: str | None = Field(
        default=None, description="Synthetic user message injected at session start"
    )
    watch_paths: list[str] | None = Field(default=None, description="Paths to watch for FileChanged events")
