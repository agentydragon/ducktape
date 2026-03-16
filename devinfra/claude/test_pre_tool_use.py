"""Tests for pre_tool_use hook."""

import pytest
import pytest_bazel

from devinfra.claude.claude_api.hooks.pre_tool_use import PermissionDecision, PreToolUseInput
from devinfra.claude.pre_tool_use import ALWAYS_ALLOW_COMMANDS, evaluate

_COMMON = {
    "session_id": "test-session",
    "transcript_path": "/tmp/transcript.jsonl",
    "cwd": "/tmp",
    "permission_mode": "default",
    "hook_event_name": "PreToolUse",
    "tool_use_id": "toolu_test123",
}


class TestEvaluate:
    @pytest.mark.parametrize("command", sorted(ALWAYS_ALLOW_COMMANDS))
    def test_allowed_bash_command_returns_allow(self, command: str) -> None:
        hook_input = PreToolUseInput(**_COMMON, tool_name="Bash", tool_input={"command": command})
        result = evaluate(hook_input)
        assert result.hook_specific_output is not None
        assert result.hook_specific_output.permission_decision == PermissionDecision.ALLOW

    def test_output_serializes_to_camel_case(self) -> None:
        hook_input = PreToolUseInput(**_COMMON, tool_name="Bash", tool_input={"command": "echo hello world"})
        result = evaluate(hook_input)
        json_output = result.model_dump_json(by_alias=True)
        assert "hookSpecificOutput" in json_output
        assert "permissionDecision" in json_output
        assert "hook_specific_output" not in json_output

    @pytest.mark.parametrize(
        ("tool_name", "tool_input"),
        [("Bash", {"command": "rm -rf /"}), ("Read", {"file_path": "/etc/passwd"}), ("Bash", {})],
        ids=["unknown-bash-command", "non-bash-tool", "bash-without-command"],
    )
    def test_returns_allow_for_unmatched(self, tool_name: str, tool_input: dict) -> None:
        hook_input = PreToolUseInput(**_COMMON, tool_name=tool_name, tool_input=tool_input)
        result = evaluate(hook_input)
        assert result.hook_specific_output is not None
        assert result.hook_specific_output.permission_decision == PermissionDecision.ALLOW


if __name__ == "__main__":
    pytest_bazel.main()
