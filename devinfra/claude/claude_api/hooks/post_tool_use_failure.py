"""Pydantic models for Claude Code PostToolUseFailure hook."""

from typing import Any, Literal

from pydantic import Field

from devinfra.claude.claude_api.hooks.common import CamelModel, HookInputBase, HookOutputBase


class PostToolUseFailureInput(HookInputBase):
    hook_event_name: Literal["PostToolUseFailure"] = "PostToolUseFailure"
    tool_name: str
    tool_input: dict[str, Any]
    tool_use_id: str
    error: str
    is_interrupt: bool | None = Field(default=None, description="Whether failure was due to user interruption")


class PostToolUseFailureHookSpecificOutput(CamelModel):
    hook_event_name: Literal["PostToolUseFailure"] = "PostToolUseFailure"
    additional_context: str | None = None


class PostToolUseFailureOutput(HookOutputBase):
    hook_specific_output: PostToolUseFailureHookSpecificOutput | None = None
