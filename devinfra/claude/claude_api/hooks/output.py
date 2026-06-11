"""Unified hook output model matching Claude Code's Zod hookOutput schema.

Claude Code defines a single ``hookOutput`` object with an optional discriminated
``hookSpecificOutput`` union (keyed on ``hookEventName``). This module provides the
Python equivalent: ``HookOutput`` with ``hook_specific_output: AnyHookSpecificOutput | None``.
"""

from typing import Annotated, Literal

from pydantic import Discriminator, Field, model_validator

from devinfra.claude.claude_api.hooks.common import CamelModel
from devinfra.claude.claude_api.hooks.cwd_changed import CwdChangedHookSpecificOutput
from devinfra.claude.claude_api.hooks.elicitation import (
    ElicitationHookSpecificOutput,
    ElicitationResultHookSpecificOutput,
)
from devinfra.claude.claude_api.hooks.file_changed import FileChangedHookSpecificOutput
from devinfra.claude.claude_api.hooks.notification import NotificationHookSpecificOutput
from devinfra.claude.claude_api.hooks.permission_denied import PermissionDeniedHookSpecificOutput
from devinfra.claude.claude_api.hooks.permission_request import PermissionRequestHookSpecificOutput
from devinfra.claude.claude_api.hooks.post_tool_use import PostToolUseHookSpecificOutput
from devinfra.claude.claude_api.hooks.post_tool_use_failure import PostToolUseFailureHookSpecificOutput
from devinfra.claude.claude_api.hooks.pre_tool_use import PreToolUseHookSpecificOutput
from devinfra.claude.claude_api.hooks.session_start import SessionStartHookSpecificOutput
from devinfra.claude.claude_api.hooks.setup import SetupHookSpecificOutput
from devinfra.claude.claude_api.hooks.subagent_start import SubagentStartHookSpecificOutput
from devinfra.claude.claude_api.hooks.user_prompt_submit import UserPromptSubmitHookSpecificOutput
from devinfra.claude.claude_api.hooks.worktree_create import WorktreeCreateHookSpecificOutput

AnyHookSpecificOutput = Annotated[
    PreToolUseHookSpecificOutput
    | PostToolUseHookSpecificOutput
    | PostToolUseFailureHookSpecificOutput
    | PermissionDeniedHookSpecificOutput
    | PermissionRequestHookSpecificOutput
    | NotificationHookSpecificOutput
    | SessionStartHookSpecificOutput
    | SetupHookSpecificOutput
    | SubagentStartHookSpecificOutput
    | UserPromptSubmitHookSpecificOutput
    | ElicitationHookSpecificOutput
    | ElicitationResultHookSpecificOutput
    | CwdChangedHookSpecificOutput
    | FileChangedHookSpecificOutput
    | WorktreeCreateHookSpecificOutput,
    Discriminator("hook_event_name"),
]


class HookOutput(CamelModel):
    """Hook output matching Claude Code's Zod ``hookOutput`` schema.

    All hook handlers return this type. The ``hook_specific_output`` field carries
    per-hook data via a discriminated union on ``hookEventName``.
    """

    continue_: bool = Field(default=True, alias="continue")
    suppress_output: bool = False
    stop_reason: str | None = None
    decision: Literal["approve", "block"] | None = Field(
        default=None,
        description="Legacy generic decision. 'approve' → allow, 'block' → deny + blockingError. "
        "Shell hooks: exit code 2 is equivalent to decision='block'.",
    )
    reason: str | None = None
    system_message: str | None = Field(
        default=None,
        description="REPL hooks only — injected into model conversation. "
        "Non-REPL hooks deliver this to the UI notification callback; model never sees it.",
    )
    hook_specific_output: AnyHookSpecificOutput | None = None

    @model_validator(mode="after")
    def _validate_stop_reason(self) -> "HookOutput":
        if self.stop_reason is not None and self.continue_:
            raise ValueError("stop_reason requires continue=false")
        return self


class HookResponse(CamelModel):
    """Internal daemon response envelope returned by the Rust `/hook` RPC."""

    output: HookOutput | None = Field(default=None, description="Typed hook output. None for noops.")
