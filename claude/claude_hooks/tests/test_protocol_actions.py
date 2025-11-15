"""Tests for protocol actions and conversion to Claude Code JSON format.

Tests the layered architecture:
1. Dataclass action creation with programmer-friendly parameters
2. Protocol conversion via to_protocol() methods
3. Exact JSON equality assertions to ensure protocol compliance
"""

from claude_hooks.actions import (
    NotificationAck,
    PostToolContinue,
    PostToolFeedbackToClaude,
    PostToolStop,
    PreCompactHandle,
    PreToolApprove,
    PreToolBlock,
    PreToolDefer,
    PreToolStop,
    StopAllow,
    StopForceContinue,
    SubagentStopAllow,
    SubagentStopForceContinue,
    UserPromptSubmitAllow,
    UserPromptSubmitBlock,
)


class TestPreToolApprove:
    def test_minimal_approve(self):
        assert PreToolApprove().to_protocol() == {"decision": "approve"}

    def test_approve_with_message_to_user(self):
        assert PreToolApprove(message_to_user="Operation approved").to_protocol() == {
            "decision": "approve",
            "reason": "Operation approved",
        }

    def test_approve_with_hide_from_transcript(self):
        assert PreToolApprove(hide_from_transcript=True).to_protocol() == {
            "decision": "approve",
            "suppressOutput": True,
        }

    def test_approve_with_all_options(self):
        assert PreToolApprove(message_to_user="All good", hide_from_transcript=True).to_protocol() == {
            "decision": "approve",
            "reason": "All good",
            "suppressOutput": True,
        }


class TestPreToolBlock:
    def test_block_minimal(self):
        assert PreToolBlock(feedback_to_claude="Unsafe command").to_protocol() == {
            "decision": "block",
            "reason": "Unsafe command",
        }

    def test_block_with_hide_from_transcript(self):
        assert PreToolBlock(feedback_to_claude="Blocked", hide_from_transcript=True).to_protocol() == {
            "decision": "block",
            "reason": "Blocked",
            "suppressOutput": True,
        }


class TestPreToolStop:
    def test_stop_minimal(self):
        assert PreToolStop(feedback_to_claude="Critical error", message_to_user="System halted").to_protocol() == {
            "decision": "block",
            "reason": "Critical error",
            "continue": False,
            "stopReason": "System halted",
        }

    def test_stop_with_hide_from_transcript(self):
        assert PreToolStop(
            feedback_to_claude="Error", message_to_user="Stopped", hide_from_transcript=True
        ).to_protocol() == {
            "decision": "block",
            "reason": "Error",
            "continue": False,
            "stopReason": "Stopped",
            "suppressOutput": True,
        }


class TestPreToolDefer:
    def test_defer_returns_empty_dict(self):
        assert PreToolDefer().to_protocol() == {}


class TestPostToolContinue:
    def test_continue_returns_empty_dict(self):
        assert PostToolContinue().to_protocol() == {}


class TestPostToolFeedbackToClaude:
    def test_feedback_to_claude_minimal(self):
        assert PostToolFeedbackToClaude(feedback_to_claude="Fix this issue").to_protocol() == {
            "decision": "block",
            "reason": "Fix this issue",
        }

    def test_feedback_to_claude_with_hide_from_transcript(self):
        assert PostToolFeedbackToClaude(
            feedback_to_claude="Error detected", hide_from_transcript=True
        ).to_protocol() == {"decision": "block", "reason": "Error detected", "suppressOutput": True}


class TestPostToolStop:
    def test_stop_minimal(self):
        assert PostToolStop(message_to_user="System halted").to_protocol() == {
            "continue": False,
            "stopReason": "System halted",
        }

    def test_stop_with_hide_from_transcript(self):
        assert PostToolStop(message_to_user="Processing stopped", hide_from_transcript=True).to_protocol() == {
            "continue": False,
            "stopReason": "Processing stopped",
            "suppressOutput": True,
        }


class TestUserPromptSubmitAllow:
    def test_allow_returns_empty_dict(self):
        assert UserPromptSubmitAllow().to_protocol() == {}


class TestUserPromptSubmitBlock:
    def test_block_minimal(self):
        assert UserPromptSubmitBlock(message_to_user="Invalid request").to_protocol() == {
            "decision": "block",
            "reason": "Invalid request",
        }

    def test_block_with_hide_from_transcript(self):
        assert UserPromptSubmitBlock(message_to_user="Blocked prompt", hide_from_transcript=True).to_protocol() == {
            "decision": "block",
            "reason": "Blocked prompt",
            "suppressOutput": True,
        }


class TestStopAllow:
    def test_allow_returns_empty_dict(self):
        assert StopAllow().to_protocol() == {}


class TestStopForceContinue:
    def test_force_continue_minimal(self):
        assert StopForceContinue(instructions_to_claude="Fix remaining issues").to_protocol() == {
            "decision": "block",
            "reason": "Fix remaining issues",
        }

    def test_force_continue_with_hide_from_transcript(self):
        assert StopForceContinue(
            instructions_to_claude="Continue processing", hide_from_transcript=True
        ).to_protocol() == {"decision": "block", "reason": "Continue processing", "suppressOutput": True}


class TestSubagentStopAllow:
    def test_allow_returns_empty_dict(self):
        assert SubagentStopAllow().to_protocol() == {}


class TestSubagentStopForceContinue:
    def test_force_continue_minimal(self):
        assert SubagentStopForceContinue(instructions_to_subagent="Complete the analysis").to_protocol() == {
            "decision": "block",
            "reason": "Complete the analysis",
        }

    def test_force_continue_with_hide_from_transcript(self):
        assert SubagentStopForceContinue(
            instructions_to_subagent="Finish the task", hide_from_transcript=True
        ).to_protocol() == {"decision": "block", "reason": "Finish the task", "suppressOutput": True}


class TestNotificationAck:
    def test_ack_returns_empty_dict(self):
        assert NotificationAck().to_protocol() == {}

    def test_ack_with_hide_from_transcript(self):
        assert NotificationAck(hide_from_transcript=True).to_protocol() == {"suppressOutput": True}


class TestPreCompactHandle:
    def test_handle_returns_empty_dict(self):
        assert PreCompactHandle().to_protocol() == {}

    def test_handle_with_hide_from_transcript(self):
        assert PreCompactHandle(hide_from_transcript=True).to_protocol() == {"suppressOutput": True}
