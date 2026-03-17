"""Discriminated union of all Claude Code hook inputs.

Parsed once in hook_dispatch.py, then isinstance/match dispatches to the
appropriate handler. Uses hook_event_name as the Pydantic discriminator.
"""

from typing import Annotated

from pydantic import Discriminator

from devinfra.claude.claude_api.hooks.config_change import ConfigChangeInput
from devinfra.claude.claude_api.hooks.elicitation import ElicitationInput, ElicitationResultInput
from devinfra.claude.claude_api.hooks.instructions_loaded import InstructionsLoadedInput
from devinfra.claude.claude_api.hooks.notification import NotificationInput
from devinfra.claude.claude_api.hooks.permission_request import PermissionRequestInput
from devinfra.claude.claude_api.hooks.post_compact import PostCompactInput
from devinfra.claude.claude_api.hooks.post_tool_use import PostToolUseInput
from devinfra.claude.claude_api.hooks.post_tool_use_failure import PostToolUseFailureInput
from devinfra.claude.claude_api.hooks.pre_compact import PreCompactInput
from devinfra.claude.claude_api.hooks.pre_tool_use import PreToolUseInput
from devinfra.claude.claude_api.hooks.session_end import SessionEndInput
from devinfra.claude.claude_api.hooks.session_start import SessionStartHookInput
from devinfra.claude.claude_api.hooks.setup import SetupInput
from devinfra.claude.claude_api.hooks.stop import StopInput
from devinfra.claude.claude_api.hooks.subagent_start import SubagentStartInput
from devinfra.claude.claude_api.hooks.subagent_stop import SubagentStopInput
from devinfra.claude.claude_api.hooks.task_completed import TaskCompletedInput
from devinfra.claude.claude_api.hooks.teammate_idle import TeammateIdleInput
from devinfra.claude.claude_api.hooks.user_prompt_submit import UserPromptSubmitInput
from devinfra.claude.claude_api.hooks.worktree_create import WorktreeCreateInput
from devinfra.claude.claude_api.hooks.worktree_remove import WorktreeRemoveInput

AnyHookInput = Annotated[
    SessionStartHookInput
    | SetupInput
    | PreToolUseInput
    | PostToolUseInput
    | UserPromptSubmitInput
    | NotificationInput
    | StopInput
    | SubagentStartInput
    | SubagentStopInput
    | PostToolUseFailureInput
    | PermissionRequestInput
    | ElicitationInput
    | ElicitationResultInput
    | ConfigChangeInput
    | PreCompactInput
    | PostCompactInput
    | InstructionsLoadedInput
    | WorktreeCreateInput
    | WorktreeRemoveInput
    | SessionEndInput
    | TeammateIdleInput
    | TaskCompletedInput,
    Discriminator("hook_event_name"),
]
