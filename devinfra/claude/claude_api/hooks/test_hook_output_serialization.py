"""Regression tests for HookOutput serialization through HookResponse.

These tests verify that hookSpecificOutput survives the full serialization
round-trip through HookResponse — the bug that was silently dropping
additionalContext, worktreePath, permissionDecision, etc.
"""

import json
from typing import Any

import pytest
import pytest_bazel

from devinfra.claude.claude_api.hooks.output import HookOutput, HookResponse
from devinfra.claude.claude_api.hooks.post_tool_use import PostToolUseHookSpecificOutput
from devinfra.claude.claude_api.hooks.pre_tool_use import PermissionDecision, PreToolUseHookSpecificOutput
from devinfra.claude.claude_api.hooks.session_start import SessionStartHookSpecificOutput
from devinfra.claude.claude_api.hooks.worktree_create import WorktreeCreateHookSpecificOutput


def _roundtrip(output: HookOutput) -> dict[str, Any]:
    """Serialize through HookResponse and parse back to dict."""
    resp = HookResponse(output=output)
    wire = resp.model_dump_json(by_alias=True, exclude_none=True)
    result: dict[str, Any] = json.loads(wire)["output"]
    return result


def test_session_start_additional_context_survives_roundtrip() -> None:
    output = HookOutput(hook_specific_output=SessionStartHookSpecificOutput(additional_context="session context here"))
    parsed = _roundtrip(output)
    assert parsed["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert parsed["hookSpecificOutput"]["additionalContext"] == "session context here"


def test_worktree_create_path_survives_roundtrip() -> None:
    output = HookOutput(hook_specific_output=WorktreeCreateHookSpecificOutput(worktree_path="/tmp/wt/test"))
    parsed = _roundtrip(output)
    assert parsed["hookSpecificOutput"]["hookEventName"] == "WorktreeCreate"
    assert parsed["hookSpecificOutput"]["worktreePath"] == "/tmp/wt/test"


def test_post_tool_use_mcp_alias_survives_roundtrip() -> None:
    output = HookOutput(
        hook_specific_output=PostToolUseHookSpecificOutput(
            additional_context="lint issues",
            updated_mcp_tool_output={"result": "modified"},  # pyright: ignore[reportCallIssue]
        )
    )
    parsed = _roundtrip(output)
    assert parsed["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
    assert parsed["hookSpecificOutput"]["additionalContext"] == "lint issues"
    assert parsed["hookSpecificOutput"]["updatedMCPToolOutput"] == {"result": "modified"}


def test_pre_tool_use_permission_decision_survives_roundtrip() -> None:
    output = HookOutput(
        hook_specific_output=PreToolUseHookSpecificOutput(
            permission_decision=PermissionDecision.ALLOW, permission_decision_reason="Command in always-allow list"
        )
    )
    parsed = _roundtrip(output)
    assert parsed["hookSpecificOutput"]["permissionDecision"] == "allow"


def test_noop_output_has_no_hook_specific_output() -> None:
    output = HookOutput()
    parsed = _roundtrip(output)
    assert "hookSpecificOutput" not in parsed


def test_deserialization_roundtrip_preserves_discriminated_union() -> None:
    output = HookOutput(hook_specific_output=SessionStartHookSpecificOutput(additional_context="ctx"))
    resp = HookResponse(output=output)
    wire = resp.model_dump_json(by_alias=True, exclude_none=True)
    restored = HookResponse.model_validate_json(wire)
    assert restored.output is not None
    assert restored.output.hook_specific_output is not None
    assert isinstance(restored.output.hook_specific_output, SessionStartHookSpecificOutput)
    assert restored.output.hook_specific_output.additional_context == "ctx"


def test_stop_reason_requires_continue_false() -> None:
    with pytest.raises(Exception, match="stop_reason requires continue=false"):
        HookOutput(stop_reason="done", continue_=True)


def test_stop_reason_with_continue_false() -> None:
    out = HookOutput(stop_reason="done", continue_=False)
    assert out.stop_reason == "done"
    assert out.continue_ is False


if __name__ == "__main__":
    pytest_bazel.main()
