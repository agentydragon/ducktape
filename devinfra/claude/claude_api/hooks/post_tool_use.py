"""Pydantic models for Claude Code PostToolUse hook.

See https://code.claude.com/docs/en/hooks for the full API spec.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, model_validator

from devinfra.claude.claude_api.hooks.common import CamelModel, HookInputBase, HookOutputBase


class PostToolUseInput(HookInputBase):
    hook_event_name: Literal["PostToolUse"] = "PostToolUse"
    tool_name: str
    tool_input: dict[str, Any]
    tool_use_id: str
    tool_response: Any


class PostToolUseHookSpecificOutput(CamelModel):
    """Nested hookSpecificOutput for PostToolUse."""

    hook_event_name: Literal["PostToolUse"] = "PostToolUse"
    additional_context: str | None = Field(default=None, description="Non-blocking extra context for Claude")
    updated_mcp_tool_output: Any | None = Field(
        default=None, alias="updatedMCPToolOutput", description="MCP tools only — replaces the tool's output"
    )


class PostToolUseOutput(HookOutputBase):
    """PostToolUse hook output per Claude Code API."""

    hook_specific_output: PostToolUseHookSpecificOutput | None = None

    @model_validator(mode="after")
    def _validate_stop_reason(self) -> PostToolUseOutput:
        if self.stop_reason is not None and self.continue_:
            raise ValueError("stop_reason requires continue=false")
        return self
