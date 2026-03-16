"""Pydantic models for Claude Code Setup hook."""

from enum import StrEnum
from typing import Literal

from devinfra.claude.claude_api.hooks.common import CamelModel, HookInputBase, HookOutputBase


class SetupTrigger(StrEnum):
    INIT = "init"
    MAINTENANCE = "maintenance"


class SetupInput(HookInputBase):
    hook_event_name: Literal["Setup"] = "Setup"
    trigger: SetupTrigger


class SetupHookSpecificOutput(CamelModel):
    hook_event_name: Literal["Setup"] = "Setup"
    additional_context: str | None = None


class SetupOutput(HookOutputBase):
    hook_specific_output: SetupHookSpecificOutput | None = None
