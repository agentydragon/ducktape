"""Pydantic models for Claude Code SessionStart hook.

See https://code.claude.com/docs/en/hooks for the full API spec.
"""

from enum import StrEnum
from typing import Literal

from pydantic import Field

from devinfra.claude.claude_api.hooks.common import CamelModel, HookInputBase, HookOutputBase


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
    initial_user_message: str | None = Field(default=None, description="Inject an initial user message")
    watch_paths: list[str] | None = Field(default=None, description="Register paths to watch for FileChanged events")


class SessionStartOutput(HookOutputBase):
    """SessionStart hook stdout JSON output."""

    hook_specific_output: SessionStartHookSpecificOutput | None = None
