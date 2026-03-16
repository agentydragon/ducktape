"""Pydantic models for Claude Code Stop hook."""

from typing import Literal

from devinfra.claude.claude_api.hooks.common import HookInputBase, HookOutputBase


class StopInput(HookInputBase):
    hook_event_name: Literal["Stop"] = "Stop"
    stop_hook_active: bool
    last_assistant_message: str


class StopOutput(HookOutputBase):
    pass
