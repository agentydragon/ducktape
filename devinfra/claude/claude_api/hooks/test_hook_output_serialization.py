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
from devinfra.claude.claude_api.hooks.session_start import SessionStartHookSpecificOutput


def _roundtrip(output: HookOutput) -> dict[str, Any]:
    """Serialize through HookResponse and parse back to dict."""
    resp = HookResponse(output=output)
    wire = resp.model_dump_json(by_alias=True, exclude_none=True)
    result: dict[str, Any] = json.loads(wire)["output"]
    return result


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
