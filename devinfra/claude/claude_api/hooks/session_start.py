"""Pydantic models for Claude Code SessionStart hook.

See https://code.claude.com/docs/en/hooks for the full API spec.
"""

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
    agent_type: str | None = Field(default=None, description="Present only when started with --agent")


class SessionStartHookSpecificOutput(CamelModel):
    hook_event_name: Literal["SessionStart"] = "SessionStart"
    additional_context: str | None = Field(default=None, description="Context added to Claude's system prompt")


class SessionStartOutput(CamelModel):
    """SessionStart hook stdout JSON output."""

    continue_: bool = Field(default=True, alias="continue", description="False to stop Claude entirely")
    stop_reason: str | None = Field(default=None, description="User-visible message when continue=false")
    suppress_output: bool = Field(default=False, description="Hide from transcript mode output")
    system_message: str | None = Field(default=None, description="Warning shown to user")
    hook_specific_output: SessionStartHookSpecificOutput | None = None
