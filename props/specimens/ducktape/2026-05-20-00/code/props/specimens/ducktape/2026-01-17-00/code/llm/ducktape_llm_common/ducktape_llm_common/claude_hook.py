"""Base class for Claude Code hooks with convenient entrypoint."""

import json
import logging
import sys
import uuid
from typing import TYPE_CHECKING, ClassVar, NoReturn

from pydantic import TypeAdapter

if TYPE_CHECKING:
    from pydantic import BaseModel

from .claude_code_api import (
    HookRequest,
    NotificationRequest,
    PostToolUseRequest,
    PreCompactRequest,
    PreToolUseRequest,
    StopRequest,
    SubagentStopRequest,
)
from .claude_outcomes import (
    HookError,
    HookOutcome,
    NotificationAcknowledge,
    NotificationOutcome,
    PostToolOutcome,
    PostToolSuccess,
    PreCompactAllow,
    PreCompactOutcome,
    PreToolNoOpinion,
    PreToolOutcome,
    StopAllow,
    StopOutcome,
    SubagentStopAllow,
    SubagentStopOutcome,
)
from .hook_logging import InvocationID, get_session_logger

_HookRequestAdapter: TypeAdapter[HookRequest] = TypeAdapter(HookRequest)


def _emit_and_exit(response: "BaseModel") -> NoReturn:
    """Serialize hook response to JSON, print to stdout, exit 0."""
    print(response.model_dump_json(by_alias=True))
    sys.exit(0)


class ClaudeCodeHookBase:
    """
    Base class for implementing Claude Code hooks.

    Automatically provides session-scoped logging via self.logger.
    Subclasses must define hook_name and implement hook methods.

    Example:
        class MyClaudeHook(ClaudeCodeHookBase):
            hook_name = "my-hook"

            def pre_tool_use(self, request: PreToolUseRequest) -> PreToolOutcome:
                if isinstance(request.tool_call, BashToolCall) and "rm -rf" in request.tool_call.command:
                    return PreToolDeny("Dangerous command blocked")
                return PreToolApprove()

        if __name__ == '__main__':
            MyClaudeHook.entrypoint()
    """

    hook_name: ClassVar[str]

    def __init__(self) -> None:
        self.logger: logging.Logger | None = None

    def pre_tool_use(self, request: PreToolUseRequest) -> PreToolOutcome:
        """Handle PreToolUse hook. Default: no opinion (let permission system decide)."""
        return PreToolNoOpinion()

    def post_tool_use(self, request: PostToolUseRequest) -> PostToolOutcome:
        """Handle PostToolUse hook. Default: success with no message."""
        return PostToolSuccess()

    def notification(self, request: NotificationRequest) -> NotificationOutcome:
        """Handle Notification hook. Default: acknowledge."""
        return NotificationAcknowledge()

    def stop(self, request: StopRequest) -> StopOutcome:
        """Handle Stop hook. Default: allow stop."""
        return StopAllow()

    def subagent_stop(self, request: SubagentStopRequest) -> SubagentStopOutcome:
        """Handle SubagentStop hook. Default: allow stop."""
        return SubagentStopAllow()

    def pre_compact(self, request: PreCompactRequest) -> PreCompactOutcome:
        """Handle PreCompact hook. Default: allow compaction."""
        return PreCompactAllow()

    @classmethod
    def entrypoint(cls) -> None:
        """
        Main entrypoint for Claude Code hooks.

        Reads JSON from stdin, dispatches to appropriate method,
        returns JSON to stdout, exits with code 0.

        Automatically sets up session-scoped logging.

        Usage:
            if __name__ == '__main__':
                MyClaudeHook.entrypoint()
        """
        try:
            input_data = json.load(sys.stdin)
            hook_instance = cls()
            request = _HookRequestAdapter.validate_python(input_data)

            invocation_id = InvocationID(uuid.uuid4())

            # Set up logger for this session/invocation
            logger = get_session_logger(hook_instance.hook_name, request.session_id, invocation_id)
            hook_instance.logger = logger

            # Log the input
            logger.info(
                "Hook input", extra={"hook_event": request.hook_event_name, "request_data": request.model_dump()}
            )

            try:
                # Dispatch to appropriate method using discriminated union
                outcome: HookOutcome
                if isinstance(request, PreToolUseRequest):
                    outcome = hook_instance.pre_tool_use(request)
                elif isinstance(request, PostToolUseRequest):
                    outcome = hook_instance.post_tool_use(request)
                elif isinstance(request, NotificationRequest):
                    outcome = hook_instance.notification(request)
                elif isinstance(request, StopRequest):
                    outcome = hook_instance.stop(request)
                elif isinstance(request, SubagentStopRequest):
                    outcome = hook_instance.subagent_stop(request)
                elif isinstance(request, PreCompactRequest):
                    outcome = hook_instance.pre_compact(request)
                else:
                    raise ValueError(f"Unknown request type: {type(request)}")

                # Convert to response once for both logging and return
                response = outcome.to_claude_response()
                logger.info("Hook output", extra={"response_data": response.model_dump()})

            except Exception:
                # Log the exception
                logger.error("Hook execution failed", exc_info=True)
                raise

            _emit_and_exit(response)

        except Exception as e:
            _emit_and_exit(HookError(f"Hook execution failed: {e!s}").to_claude_response())
