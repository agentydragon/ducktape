"""Discriminated union of all Claude Code hook outputs.

Mirrors dispatch_input.py but for output types. Not all hooks have output
models — hooks with pass-through HookOutputBase are input-only (PreCompact,
PostCompact, SessionEnd, InstructionsLoaded, WorktreeCreate, WorktreeRemove,
TeammateIdle, TaskCompleted, StopFailure, TaskCreated).

Output models don't have a top-level discriminator field like inputs do
(hook_event_name is on hook_specific_output, not on the output itself).
This module provides a plain Union type for type-checking and documentation.
"""

from devinfra.claude.claude_api.hooks.config_change import ConfigChangeOutput
from devinfra.claude.claude_api.hooks.cwd_changed import CwdChangedOutput
from devinfra.claude.claude_api.hooks.elicitation import ElicitationOutput, ElicitationResultOutput
from devinfra.claude.claude_api.hooks.file_changed import FileChangedOutput
from devinfra.claude.claude_api.hooks.notification import NotificationOutput
from devinfra.claude.claude_api.hooks.permission_request import PermissionRequestOutput
from devinfra.claude.claude_api.hooks.post_tool_use import PostToolUseOutput
from devinfra.claude.claude_api.hooks.post_tool_use_failure import PostToolUseFailureOutput
from devinfra.claude.claude_api.hooks.pre_tool_use import PreToolUseOutput
from devinfra.claude.claude_api.hooks.session_start import SessionStartOutput
from devinfra.claude.claude_api.hooks.setup import SetupOutput
from devinfra.claude.claude_api.hooks.stop import StopOutput
from devinfra.claude.claude_api.hooks.subagent_start import SubagentStartOutput
from devinfra.claude.claude_api.hooks.subagent_stop import SubagentStopOutput
from devinfra.claude.claude_api.hooks.user_prompt_submit import UserPromptSubmitOutput

AnyHookOutput = (
    PreToolUseOutput
    | PostToolUseOutput
    | PostToolUseFailureOutput
    | PermissionRequestOutput
    | NotificationOutput
    | SessionStartOutput
    | SetupOutput
    | StopOutput
    | UserPromptSubmitOutput
    | SubagentStartOutput
    | SubagentStopOutput
    | ElicitationOutput
    | ElicitationResultOutput
    | ConfigChangeOutput
    | CwdChangedOutput
    | FileChangedOutput
)
