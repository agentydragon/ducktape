"""Pydantic models for Claude Code SessionEnd hook."""

from enum import StrEnum
from typing import Literal

from devinfra.claude.claude_api.hooks.common import HookInputBase


class SessionEndReason(StrEnum):
    CLEAR = "clear"
    LOGOUT = "logout"
    PROMPT_INPUT_EXIT = "prompt_input_exit"
    OTHER = "other"
    RESUME = "resume"  # Added in v2.1.87
    # CLEANUP(2026-03-30): Removed in v2.1.87 (was in v2.1.76). Keep accepting
    # for backwards compat until we're sure no older Claude Code versions send it.
    BYPASS_PERMISSIONS_DISABLED = "bypass_permissions_disabled"


class SessionEndInput(HookInputBase):
    hook_event_name: Literal["SessionEnd"] = "SessionEnd"
    reason: SessionEndReason
