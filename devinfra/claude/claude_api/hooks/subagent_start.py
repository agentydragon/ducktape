"""Pydantic models for Claude Code SubagentStart hook."""

from typing import Literal

from devinfra.claude.claude_api.hooks.common import CamelModel, HookInputBase, HookOutputBase


class SubagentStartInput(HookInputBase):
    hook_event_name: Literal["SubagentStart"] = "SubagentStart"
    agent_id: str
    agent_type: str


class SubagentStartHookSpecificOutput(CamelModel):
    hook_event_name: Literal["SubagentStart"] = "SubagentStart"
    additional_context: str | None = None


class SubagentStartOutput(HookOutputBase):
    hook_specific_output: SubagentStartHookSpecificOutput | None = None
