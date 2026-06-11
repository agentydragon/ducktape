"""Pydantic models for Claude Code WorktreeRemove hook (non-REPL)."""

from pathlib import Path
from typing import Literal

from devinfra.claude.claude_api.hooks.common import HookInputBase


class WorktreeRemoveInput(HookInputBase):
    hook_event_name: Literal["WorktreeRemove"] = "WorktreeRemove"
    worktree_path: Path
