"""Tests for protocol actions and conversion to Claude Code JSON format.

Tests the layered architecture:
1. Dataclass action creation with programmer-friendly parameters
2. Protocol conversion via to_protocol() methods
3. Exact JSON equality assertions to ensure protocol compliance
"""

import pytest_bazel

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


def test_pre_tool_approve_minimal():
    assert PreToolApprove().to_protocol() == {"decision": "approve"}


def test_pre_tool_approve_with_message_to_user():
    assert PreToolApprove(message_to_user="Operation approved").to_protocol() == {
        "decision": "approve",
        "reason": "Operation approved",
    }


def test_pre_tool_approve_with_hide_from_transcript():
    assert PreToolApprove(hide_from_transcript=True).to_protocol() == {"decision": "approve", "suppressOutput": True}


def test_pre_tool_approve_with_all_options():
    assert PreToolApprove(message_to_user="All good", hide_from_transcript=True).to_protocol() == {
        "decision": "approve",
        "reason": "All good",
        "suppressOutput": True,
    }


def test_pre_tool_block_minimal():
    assert PreToolBlock(feedback_to_claude="Unsafe command").to_protocol() == {
        "decision": "block",
        "reason": "Unsafe command",
    }


def test_pre_tool_block_with_hide_from_transcript():
    assert PreToolBlock(feedback_to_claude="Blocked", hide_from_transcript=True).to_protocol() == {
        "decision": "block",
        "reason": "Blocked",
        "suppressOutput": True,
    }


def test_pre_tool_stop_minimal():
    assert PreToolStop(feedback_to_claude="Critical error", message_to_user="System halted").to_protocol() == {
        "decision": "block",
        "reason": "Critical error",
        "continue": False,
        "stopReason": "System halted",
    }


def test_pre_tool_stop_with_hide_from_transcript():
    assert PreToolStop(
        feedback_to_claude="Error", message_to_user="Stopped", hide_from_transcript=True
    ).to_protocol() == {
        "decision": "block",
        "reason": "Error",
        "continue": False,
        "stopReason": "Stopped",
        "suppressOutput": True,
    }


def test_pre_tool_defer_returns_empty_dict():
    assert PreToolDefer().to_protocol() == {}


def test_post_tool_continue_returns_empty_dict():
    assert PostToolContinue().to_protocol() == {}


def test_post_tool_feedback_to_claude_minimal():
    assert PostToolFeedbackToClaude(feedback_to_claude="Fix this issue").to_protocol() == {
        "decision": "block",
        "reason": "Fix this issue",
    }


def test_post_tool_feedback_to_claude_with_hide_from_transcript():
    assert PostToolFeedbackToClaude(feedback_to_claude="Error detected", hide_from_transcript=True).to_protocol() == {
        "decision": "block",
        "reason": "Error detected",
        "suppressOutput": True,
    }


def test_post_tool_stop_minimal():
    assert PostToolStop(message_to_user="System halted").to_protocol() == {
        "continue": False,
        "stopReason": "System halted",
    }


def test_post_tool_stop_with_hide_from_transcript():
    assert PostToolStop(message_to_user="Processing stopped", hide_from_transcript=True).to_protocol() == {
        "continue": False,
        "stopReason": "Processing stopped",
        "suppressOutput": True,
    }


def test_user_prompt_submit_allow_returns_empty_dict():
    assert UserPromptSubmitAllow().to_protocol() == {}


def test_user_prompt_submit_block_minimal():
    assert UserPromptSubmitBlock(message_to_user="Invalid request").to_protocol() == {
        "decision": "block",
        "reason": "Invalid request",
    }


def test_user_prompt_submit_block_with_hide_from_transcript():
    assert UserPromptSubmitBlock(message_to_user="Blocked prompt", hide_from_transcript=True).to_protocol() == {
        "decision": "block",
        "reason": "Blocked prompt",
        "suppressOutput": True,
    }


def test_stop_allow_returns_empty_dict():
    assert StopAllow().to_protocol() == {}


def test_stop_force_continue_minimal():
    assert StopForceContinue(instructions_to_claude="Fix remaining issues").to_protocol() == {
        "decision": "block",
        "reason": "Fix remaining issues",
    }


def test_stop_force_continue_with_hide_from_transcript():
    assert StopForceContinue(instructions_to_claude="Continue processing", hide_from_transcript=True).to_protocol() == {
        "decision": "block",
        "reason": "Continue processing",
        "suppressOutput": True,
    }


def test_subagent_stop_allow_returns_empty_dict():
    assert SubagentStopAllow().to_protocol() == {}


def test_subagent_stop_force_continue_minimal():
    assert SubagentStopForceContinue(instructions_to_subagent="Complete the analysis").to_protocol() == {
        "decision": "block",
        "reason": "Complete the analysis",
    }


def test_subagent_stop_force_continue_with_hide_from_transcript():
    assert SubagentStopForceContinue(
        instructions_to_subagent="Finish the task", hide_from_transcript=True
    ).to_protocol() == {"decision": "block", "reason": "Finish the task", "suppressOutput": True}


def test_notification_ack_returns_empty_dict():
    assert NotificationAck().to_protocol() == {}


def test_notification_ack_with_hide_from_transcript():
    assert NotificationAck(hide_from_transcript=True).to_protocol() == {"suppressOutput": True}


def test_pre_compact_handle_returns_empty_dict():
    assert PreCompactHandle().to_protocol() == {}


def test_pre_compact_handle_with_hide_from_transcript():
    assert PreCompactHandle(hide_from_transcript=True).to_protocol() == {"suppressOutput": True}


if __name__ == "__main__":
    pytest_bazel.main()
