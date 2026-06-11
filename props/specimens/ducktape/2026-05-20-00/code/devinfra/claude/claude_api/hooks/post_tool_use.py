"""Pydantic models for Claude Code PostToolUse hook.

See https://code.claude.com/docs/en/hooks for the full API spec.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from devinfra.claude.claude_api.hooks.common import CamelModel, HookInputBase


class PostToolUseInput(HookInputBase):
    hook_event_name: Literal["PostToolUse"] = "PostToolUse"
    tool_name: str
    tool_input: dict[str, Any]
    tool_use_id: str
    tool_response: Any


class PostToolUseHookSpecificOutput(CamelModel):
    hook_event_name: Literal["PostToolUse"] = "PostToolUse"
    additional_context: str | None = Field(default=None, description="Non-blocking extra context for Claude")
    updated_mcp_tool_output: Any | None = Field(
        default=None,
        alias="updatedMCPToolOutput",
        description="MCP tools only — replaces the tool's output. Silently ignored for built-in tools.",
    )
