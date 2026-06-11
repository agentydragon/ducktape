"""Pydantic models for Claude Code UserPromptSubmit hook."""

from typing import Literal

from devinfra.claude.claude_api.hooks.common import CamelModel, HookInputBase


class UserPromptSubmitInput(HookInputBase):
    hook_event_name: Literal["UserPromptSubmit"] = "UserPromptSubmit"
    prompt: str


class UserPromptSubmitHookSpecificOutput(CamelModel):
    hook_event_name: Literal["UserPromptSubmit"] = "UserPromptSubmit"
    additional_context: str | None = None
