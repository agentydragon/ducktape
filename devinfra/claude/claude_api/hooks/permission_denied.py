"""Pydantic models for Claude Code PermissionDenied hook.

Fires when a tool use is denied (by the user or by a deny rule). The hook can
set ``retry: true`` in its hookSpecificOutput to make Claude retry the tool use.
"""

from typing import Any, Literal

from pydantic import Field

from devinfra.claude.claude_api.hooks.common import CamelModel, HookInputBase


class PermissionDeniedInput(HookInputBase):
    hook_event_name: Literal["PermissionDenied"] = "PermissionDenied"
    tool_name: str
    tool_input: dict[str, Any]
    tool_use_id: str
    reason: str


class PermissionDeniedHookSpecificOutput(CamelModel):
    hook_event_name: Literal["PermissionDenied"] = "PermissionDenied"
    retry: bool | None = Field(default=None, description="If true, Claude retries the denied tool use")
