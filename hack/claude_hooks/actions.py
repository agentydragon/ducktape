"""Programmer-friendly dataclass actions for Claude Code hooks.

This module provides a layered architecture:
1. Dataclass actions with clear programmer-friendly APIs
2. to_protocol() methods that convert to exact Claude Code JSON format
3. Comprehensive tests with exact JSON equality assertions (assert action.to_protocol() == {...})
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass

from typing_extensions import TypedDict

HookOutput = TypedDict(
    "HookOutput",
    {"decision": str, "reason": str, "suppressOutput": bool, "stopReason": str, "continue": bool},
    total=False,
)


@dataclass
class HookAction(ABC):
    """Base class for all hook actions."""

    @abstractmethod
    def to_protocol(self) -> HookOutput:
        """Convert to Claude Code protocol JSON format."""


@dataclass
class PreToolApprove(HookAction):
    """Approve the tool call, bypassing permission system.

    Args:
        message_to_user: Optional message shown to user (not Claude)
        hide_from_transcript: Whether to hide from transcript mode (default: show)
    """

    message_to_user: str | None = None
    hide_from_transcript: bool = False

    def to_protocol(self) -> HookOutput:
        result: HookOutput = {"decision": "approve"}
        if self.message_to_user is not None:
            result["reason"] = self.message_to_user
        if self.hide_from_transcript:
            result["suppressOutput"] = True
        return result


@dataclass
class PreToolBlock(HookAction):
    """Block this specific tool call, but Claude rollout continues.

    Args:
        feedback_to_claude: Required feedback shown to Claude for adaptation
        hide_from_transcript: Whether to hide from transcript mode (default: show)
    """

    feedback_to_claude: str
    hide_from_transcript: bool = False

    def to_protocol(self) -> HookOutput:
        result: HookOutput = {"decision": "block", "reason": self.feedback_to_claude}
        if self.hide_from_transcript:
            result["suppressOutput"] = True
        return result


@dataclass
class PreToolStop(HookAction):
    """Block the tool call AND stop the current Claude rollout.

    User can still send more messages to continue the session.

    Args:
        feedback_to_claude: Required feedback shown to Claude about why tool blocked
        message_to_user: Required message shown to user about why rollout stopped
        hide_from_transcript: Whether to hide from transcript mode (default: show)
    """

    feedback_to_claude: str
    message_to_user: str
    hide_from_transcript: bool = False

    def to_protocol(self) -> HookOutput:
        result: HookOutput = {
            "decision": "block",
            "reason": self.feedback_to_claude,
            "continue": False,
            "stopReason": self.message_to_user,
        }
        if self.hide_from_transcript:
            result["suppressOutput"] = True
        return result


@dataclass
class PreToolDefer(HookAction):
    """Defer to normal permission flow - hook has no opinion."""

    def to_protocol(self) -> HookOutput:
        # For defer, return empty dict - no decision field at all
        return {}


# Union type for all PreTool actions
PreToolAction = PreToolApprove | PreToolBlock | PreToolStop | PreToolDefer


@dataclass
class PostToolContinue(HookAction):
    """Continue normally - no intervention needed."""

    def to_protocol(self) -> HookOutput:
        # Return empty dict - no decision field at all
        return {}


@dataclass
class PostToolFeedbackToClaude(HookAction):
    """Provide automated feedback to Claude about tool results.

    Args:
        feedback_to_claude: Required feedback shown to Claude about tool results
        hide_from_transcript: Whether to hide from transcript mode (default: show)
    """

    feedback_to_claude: str
    hide_from_transcript: bool = False

    def to_protocol(self) -> HookOutput:
        result: HookOutput = {"decision": "block", "reason": self.feedback_to_claude}
        if self.hide_from_transcript:
            result["suppressOutput"] = True
        return result


@dataclass
class PostToolStop(HookAction):
    """Stop Claude processing entirely based on tool results.

    Uses continue: false which takes precedence over decision control per protocol.

    Args:
        message_to_user: Required message shown to user about why processing stopped
        hide_from_transcript: Whether to hide from transcript mode (default: show)
    """

    message_to_user: str
    hide_from_transcript: bool = False

    def to_protocol(self) -> HookOutput:
        result: HookOutput = {"continue": False, "stopReason": self.message_to_user}
        if self.hide_from_transcript:
            result["suppressOutput"] = True
        return result


# Union type for all PostTool actions
PostToolAction = PostToolContinue | PostToolFeedbackToClaude | PostToolStop


@dataclass
class UserPromptSubmitAllow(HookAction):
    """Allow prompt to proceed normally."""

    def to_protocol(self) -> HookOutput:
        # Return empty dict - no decision field at all
        return {}


@dataclass
class UserPromptSubmitBlock(HookAction):
    """Block the prompt from being processed.

    The submitted prompt is erased from context.

    Args:
        message_to_user: Required message shown to user about why prompt was blocked
        hide_from_transcript: Whether to hide from transcript mode (default: show)
    """

    message_to_user: str
    hide_from_transcript: bool = False

    def to_protocol(self) -> HookOutput:
        result: HookOutput = {"decision": "block", "reason": self.message_to_user}
        if self.hide_from_transcript:
            result["suppressOutput"] = True
        return result


# Union type for all UserPromptSubmit actions
UserPromptSubmitAction = UserPromptSubmitAllow | UserPromptSubmitBlock


@dataclass
class StopAllow(HookAction):
    """Allow Claude to stop normally."""

    def to_protocol(self) -> HookOutput:
        # Return empty dict - no decision field at all
        return {}


@dataclass
class StopForceContinue(HookAction):
    """Prevent Claude from stopping - force Claude to continue.

    Args:
        instructions_to_claude: Required instructions for Claude on how to proceed
        hide_from_transcript: Whether to hide from transcript mode (default: show)
    """

    instructions_to_claude: str
    hide_from_transcript: bool = False

    def to_protocol(self) -> HookOutput:
        result: HookOutput = {"decision": "block", "reason": self.instructions_to_claude}
        if self.hide_from_transcript:
            result["suppressOutput"] = True
        return result


# Union type for all Stop actions
StopAction = StopAllow | StopForceContinue


@dataclass
class SubagentStopAllow(HookAction):
    """Allow subagent to stop normally."""

    def to_protocol(self) -> HookOutput:
        # Return empty dict - no decision field at all
        return {}


@dataclass
class SubagentStopForceContinue(HookAction):
    """Prevent subagent from stopping - force subagent to continue.

    Args:
        instructions_to_subagent: Required instructions for subagent on how to proceed
        hide_from_transcript: Whether to hide from transcript mode (default: show)
    """

    instructions_to_subagent: str
    hide_from_transcript: bool = False

    def to_protocol(self) -> HookOutput:
        result: HookOutput = {"decision": "block", "reason": self.instructions_to_subagent}
        if self.hide_from_transcript:
            result["suppressOutput"] = True
        return result


# Union type for all SubagentStop actions
SubagentStopAction = SubagentStopAllow | SubagentStopForceContinue


@dataclass
class NotificationAck(HookAction):
    """Acknowledge notification with custom behavior (logging, desktop alerts, etc.).

    Notifications are purely reactive - no decision control available.

    Args:
        hide_from_transcript: Whether to hide from transcript mode (default: show)
    """

    hide_from_transcript: bool = False

    def to_protocol(self) -> HookOutput:
        result: HookOutput = {}
        if self.hide_from_transcript:
            result["suppressOutput"] = True
        return result


# Union type for all Notification actions
NotificationAction = NotificationAck


@dataclass
class PreCompactHandle(HookAction):
    """Handle pre-compact event with custom behavior (logging, prep work, etc.).

    PreCompact hooks are purely reactive - no decision control available.

    Args:
        hide_from_transcript: Whether to hide from transcript mode (default: show)
    """

    hide_from_transcript: bool = False

    def to_protocol(self) -> HookOutput:
        result: HookOutput = {}
        if self.hide_from_transcript:
            result["suppressOutput"] = True
        return result


# Union type for all PreCompact actions
PreCompactAction = PreCompactHandle
