"""Pydantic models for Claude Code CwdChanged hook (v2.1.87+)."""

from typing import Literal

from pydantic import Field

from devinfra.claude.claude_api.hooks.common import CamelModel, HookInputBase, HookOutputBase


class CwdChangedInput(HookInputBase):
    hook_event_name: Literal["CwdChanged"] = "CwdChanged"
    old_cwd: str
    new_cwd: str


class CwdChangedHookSpecificOutput(CamelModel):
    hook_event_name: Literal["CwdChanged"] = "CwdChanged"
    watch_paths: list[str] | None = Field(default=None, description="Register paths to watch for FileChanged events")


class CwdChangedOutput(HookOutputBase):
    hook_specific_output: CwdChangedHookSpecificOutput | None = None
