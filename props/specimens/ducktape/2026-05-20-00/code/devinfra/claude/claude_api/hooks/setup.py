"""Pydantic models for Claude Code Setup hook (non-REPL).

Lifecycle hook for one-time repository/environment initialization, separate
from SessionStart. Fires before SessionStart. Invoked via CLI flags:
``--init``, ``--init-only``, ``--maintenance``. Cannot block (exit code 2
is ignored). Receives CLAUDE_ENV_FILE.
"""

from enum import StrEnum
from typing import Literal

from pydantic import Field

from devinfra.claude.claude_api.hooks.common import CamelModel, HookInputBase


class SetupTrigger(StrEnum):
    """What triggered the Setup hook."""

    INIT = "init"
    MAINTENANCE = "maintenance"


class SetupInput(HookInputBase):
    """Input for Claude Code Setup hooks (parsed from stdin JSON)."""

    hook_event_name: Literal["Setup"] = "Setup"
    trigger: SetupTrigger = Field(description="Whether this is initial setup or periodic maintenance")


class SetupHookSpecificOutput(CamelModel):
    hook_event_name: Literal["Setup"] = "Setup"
    additional_context: str | None = Field(default=None, description="Context added to Claude's system prompt")
