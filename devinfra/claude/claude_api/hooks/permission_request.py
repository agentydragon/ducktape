"""Pydantic models for Claude Code PermissionRequest hook."""

from typing import Any, Literal

from pydantic import BaseModel, Field

from devinfra.claude.claude_api.hooks.common import CamelModel, HookInputBase, HookOutputBase


class PermissionSuggestion(BaseModel):
    type: str
    tool: str


class PermissionRequestInput(HookInputBase):
    hook_event_name: Literal["PermissionRequest"] = "PermissionRequest"
    tool_name: str
    tool_input: dict[str, Any]
    permission_suggestions: list[PermissionSuggestion] = Field(default_factory=list)


class PermissionRequestDecision(CamelModel):
    behavior: Literal["allow", "deny"]
    updated_input: dict[str, Any] | None = None
    updated_permissions: list[Any] | None = None
    message: str | None = Field(default=None, description="Reason shown when behavior='deny'")


class PermissionRequestHookSpecificOutput(CamelModel):
    hook_event_name: Literal["PermissionRequest"] = "PermissionRequest"
    decision: PermissionRequestDecision


class PermissionRequestOutput(HookOutputBase):
    hook_specific_output: PermissionRequestHookSpecificOutput | None = None
