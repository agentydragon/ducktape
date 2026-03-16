"""Pydantic models for Claude Code SubagentStop hook."""

from typing import Literal

from devinfra.claude.claude_api.hooks.common import HookInputBase, HookOutputBase


class SubagentStopInput(HookInputBase):
    hook_event_name: Literal["SubagentStop"] = "SubagentStop"
    stop_hook_active: bool
    agent_id: str
    agent_type: str
    agent_transcript_path: str
    last_assistant_message: str


class SubagentStopOutput(HookOutputBase):
    pass
