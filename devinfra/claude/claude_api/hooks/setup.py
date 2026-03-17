"""Pydantic models for Claude Code Setup hook.

Setup is a lifecycle hook for one-time repository/environment initialization,
separate from SessionStart. Key differences from SessionStart:

- **Trigger**: ``trigger`` field with ``"init"`` or ``"maintenance"`` (vs
  SessionStart's ``source`` field with ``"startup"``/``"resume"``/``"clear"``/``"compact"``).
- **When it fires**: Before SessionStart in the startup sequence. Invoked via
  hidden CLI flags: ``--init`` (trigger=init, then continue), ``--init-only``
  (trigger=init + SessionStart:startup, then exit), ``--maintenance``
  (trigger=maintenance, then continue).
- **CLAUDE_ENV_FILE**: Setup hooks receive ``CLAUDE_ENV_FILE`` in their
  environment, same as SessionStart. The env file path is
  ``<session-env-dir>/setup-hook-<session_id>.sh`` (separate from
  SessionStart's ``sessionstart-hook-<session_id>.sh``).
- **Matcher field**: ``trigger`` (matches ``"init"`` or ``"maintenance"``).
- **HTTP hooks disabled**: Like SessionStart, only command hooks are
  supported (HTTP hooks are skipped with a warning).
- **Cannot block**: Unlike most hooks, Setup cannot block the session.
  The binary says "Blocking errors are ignored" — exit code 2 (the
  standard "block" signal) has no special meaning for Setup.
- **Progress reporting**: Like SessionStart, Setup hook output is shown
  to the user during startup (not silently consumed).
- **forceSyncExecution**: ``--init-only`` runs Setup with
  ``forceSyncExecution: true``, blocking until complete.

Typical use: CI/CD ``--init-only`` to run repo setup hooks and verify the
environment, then exit. Or ``--maintenance`` for periodic background upkeep.
"""

from enum import StrEnum
from typing import Literal

from pydantic import Field

from devinfra.claude.claude_api.hooks.common import CamelModel, HookInputBase, HookOutputBase


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


class SetupOutput(HookOutputBase):
    """Setup hook stdout JSON output."""

    hook_specific_output: SetupHookSpecificOutput | None = None
