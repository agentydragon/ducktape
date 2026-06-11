"""Type-safe Claude Code hook types (user layer).

See: https://code.claude.com/docs/en/hooks
"""

from dataclasses import dataclass
from typing import Any, Literal

from .claude_code_api import (
    HookResponse,
    PermissionDecisionPayload,
    PermissionHookOutput,
    PostToolHookOutput,
    PreToolHookOutput,
    SessionID,
    SessionStartHookOutput,
    StopHookOutput,
    SubagentStopHookOutput,
    ToolCall,
    UserPromptHookOutput,
)

CompactTrigger = Literal["manual", "auto"]


# ============================================================
# User Request Types (from wire via .to_request())
# ============================================================


@dataclass(frozen=True)
class PreToolUseRequest:
    session_id: SessionID
    tool_call: ToolCall


@dataclass(frozen=True)
class PostToolUseRequest:
    session_id: SessionID
    tool_call: ToolCall
    tool_response: dict[str, Any] | None = None


@dataclass(frozen=True)
class StopRequest:
    session_id: SessionID


@dataclass(frozen=True)
class SubagentStopRequest:
    session_id: SessionID


@dataclass(frozen=True)
class NotificationRequest:
    session_id: SessionID
    message: str
    title: str | None = None


@dataclass(frozen=True)
class UserPromptSubmitRequest:
    session_id: SessionID
    prompt: str


@dataclass(frozen=True)
class PermissionRequest:
    session_id: SessionID
    tool_call: ToolCall


@dataclass(frozen=True)
class PreCompactRequest:
    session_id: SessionID
    trigger: CompactTrigger


@dataclass(frozen=True)
class SessionStartRequest:
    session_id: SessionID


@dataclass(frozen=True)
class SessionEndRequest:
    session_id: SessionID


HookRequest = (
    PreToolUseRequest
    | PostToolUseRequest
    | StopRequest
    | SubagentStopRequest
    | NotificationRequest
    | UserPromptSubmitRequest
    | PermissionRequest
    | PreCompactRequest
    | SessionStartRequest
    | SessionEndRequest
)


# ============================================================
# User Outcome Types (converted to wire via .to_wire())
# ============================================================


@dataclass(frozen=True)
class HaltExecution:
    """Stop Claude's execution entirely. Works from any hook."""

    stop_reason: str

    def to_wire(self) -> HookResponse:
        return HookResponse(continue_=False, stop_reason=self.stop_reason)


# PreToolUse Outcomes


@dataclass(frozen=True)
class PreToolAllow:
    """Bypass permission system, optionally modify input."""

    reason: str | None = None
    updated_input: dict[str, Any] | None = None

    def to_wire(self) -> HookResponse:
        return HookResponse(
            hook_specific_output=PreToolHookOutput(
                permission_decision="allow", permission_decision_reason=self.reason, updated_input=self.updated_input
            )
        )


@dataclass(frozen=True)
class PreToolDeny:
    """Block tool execution with message to Claude."""

    reason: str

    def to_wire(self) -> HookResponse:
        return HookResponse(
            hook_specific_output=PreToolHookOutput(permission_decision="deny", permission_decision_reason=self.reason)
        )


@dataclass(frozen=True)
class PreToolAsk:
    """Defer to permission UI."""

    reason: str | None = None

    def to_wire(self) -> HookResponse:
        return HookResponse(
            hook_specific_output=PreToolHookOutput(permission_decision="ask", permission_decision_reason=self.reason)
        )


@dataclass(frozen=True)
class PreToolNoOpinion:
    """Defer to default permission flow."""

    def to_wire(self) -> HookResponse:
        return HookResponse()


PreToolOutcome = PreToolAllow | PreToolDeny | PreToolAsk | PreToolNoOpinion | HaltExecution


# PostToolUse Outcomes


@dataclass(frozen=True)
class PostToolContinue:
    """Tool completed, optionally add context."""

    additional_context: str | None = None

    def to_wire(self) -> HookResponse:
        if self.additional_context:
            return HookResponse(hook_specific_output=PostToolHookOutput(additional_context=self.additional_context))
        return HookResponse()


@dataclass(frozen=True)
class PostToolBlock:
    """Send feedback to Claude."""

    reason: str

    def to_wire(self) -> HookResponse:
        return HookResponse(hook_specific_output=PostToolHookOutput(decision="block", reason=self.reason))


PostToolOutcome = PostToolContinue | PostToolBlock | HaltExecution


# Stop Outcomes


@dataclass(frozen=True)
class StopAllow:
    def to_wire(self) -> HookResponse:
        return HookResponse()


@dataclass(frozen=True)
class StopBlock:
    reason: str

    def to_wire(self) -> HookResponse:
        return HookResponse(hook_specific_output=StopHookOutput(decision="block", reason=self.reason))


StopOutcome = StopAllow | StopBlock | HaltExecution


# SubagentStop Outcomes


@dataclass(frozen=True)
class SubagentStopAllow:
    def to_wire(self) -> HookResponse:
        return HookResponse()


@dataclass(frozen=True)
class SubagentStopBlock:
    reason: str

    def to_wire(self) -> HookResponse:
        return HookResponse(hook_specific_output=SubagentStopHookOutput(decision="block", reason=self.reason))


SubagentStopOutcome = SubagentStopAllow | SubagentStopBlock | HaltExecution


# Notification Outcomes


@dataclass(frozen=True)
class NotificationAck:
    def to_wire(self) -> HookResponse:
        return HookResponse()


NotificationOutcome = NotificationAck | HaltExecution


# UserPromptSubmit Outcomes


@dataclass(frozen=True)
class UserPromptAllow:
    def to_wire(self) -> HookResponse:
        return HookResponse()


@dataclass(frozen=True)
class UserPromptBlock:
    reason: str

    def to_wire(self) -> HookResponse:
        return HookResponse(hook_specific_output=UserPromptHookOutput(decision="block", reason=self.reason))


@dataclass(frozen=True)
class UserPromptAugment:
    additional_context: str

    def to_wire(self) -> HookResponse:
        return HookResponse(hook_specific_output=UserPromptHookOutput(additional_context=self.additional_context))


UserPromptOutcome = UserPromptAllow | UserPromptBlock | UserPromptAugment | HaltExecution


# PermissionRequest Outcomes


@dataclass(frozen=True)
class PermissionAllow:
    message: str | None = None
    updated_input: dict[str, Any] | None = None

    def to_wire(self) -> HookResponse:
        return HookResponse(
            hook_specific_output=PermissionHookOutput(
                decision=PermissionDecisionPayload(
                    behavior="allow", message=self.message, updated_input=self.updated_input
                )
            )
        )


@dataclass(frozen=True)
class PermissionDeny:
    message: str | None = None
    interrupt: bool = False

    def to_wire(self) -> HookResponse:
        return HookResponse(
            continue_=not self.interrupt,
            hook_specific_output=PermissionHookOutput(
                decision=PermissionDecisionPayload(behavior="deny", message=self.message)
            ),
        )


PermissionOutcome = PermissionAllow | PermissionDeny | HaltExecution


# PreCompact Outcomes


@dataclass(frozen=True)
class PreCompactAllow:
    def to_wire(self) -> HookResponse:
        return HookResponse()


PreCompactOutcome = PreCompactAllow | HaltExecution


# SessionStart Outcomes


@dataclass(frozen=True)
class SessionStartSuccess:
    additional_context: str | None = None

    def to_wire(self) -> HookResponse:
        if self.additional_context:
            return HookResponse(hook_specific_output=SessionStartHookOutput(additional_context=self.additional_context))
        return HookResponse()


SessionStartOutcome = SessionStartSuccess | HaltExecution


# SessionEnd Outcomes


@dataclass(frozen=True)
class SessionEndAck:
    def to_wire(self) -> HookResponse:
        return HookResponse()


SessionEndOutcome = SessionEndAck | HaltExecution
