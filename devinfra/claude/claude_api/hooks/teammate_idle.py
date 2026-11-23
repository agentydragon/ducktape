"""Pydantic models for Claude Code TeammateIdle hook."""

from typing import Literal

from devinfra.claude.claude_api.hooks.hook_base import HookInputBase


class TeammateIdleInput(HookInputBase):
    hook_event_name: Literal["TeammateIdle"] = "TeammateIdle"
    teammate_name: str
    team_name: str
