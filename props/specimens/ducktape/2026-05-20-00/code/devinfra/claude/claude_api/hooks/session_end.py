"""Pydantic models for Claude Code SessionEnd hook (non-REPL)."""

from enum import StrEnum
from typing import Literal

from devinfra.claude.claude_api.hooks.common import HookInputBase


class SessionEndReason(StrEnum):
    CLEAR = "clear"
    LOGOUT = "logout"
    PROMPT_INPUT_EXIT = "prompt_input_exit"
    OTHER = "other"
    RESUME = "resume"
    BYPASS_PERMISSIONS_DISABLED = "bypass_permissions_disabled"


class SessionEndInput(HookInputBase):
    hook_event_name: Literal["SessionEnd"] = "SessionEnd"
    reason: SessionEndReason
