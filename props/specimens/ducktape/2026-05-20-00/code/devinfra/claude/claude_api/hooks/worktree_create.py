"""Pydantic models for Claude Code WorktreeCreate hook (non-REPL)."""

from typing import Literal

from pydantic import Field

from devinfra.claude.claude_api.hooks.common import CamelModel, HookInputBase


class WorktreeCreateInput(HookInputBase):
    hook_event_name: Literal["WorktreeCreate"] = "WorktreeCreate"
    name: str


class WorktreeCreateHookSpecificOutput(CamelModel):
    hook_event_name: Literal["WorktreeCreate"] = "WorktreeCreate"
    worktree_path: str = Field(description="Absolute path to the created worktree directory")
