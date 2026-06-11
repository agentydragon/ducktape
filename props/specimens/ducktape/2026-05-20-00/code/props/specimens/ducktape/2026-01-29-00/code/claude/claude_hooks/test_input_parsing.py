"""Tests for input parsing logic - verifying tool inputs are parsed correctly.

Test data collection methodology:
- All test cases are empirically collected from actual Claude Code v1.0.56 hook invocations
- JSON structures represent real-world usage patterns and tool interactions
- Some values are paraphrased (e.g., shortened commands, anonymized paths) but structure remains equivalent
- Test data is organized in testdata/hook_inputs/PostToolUse/[ToolName]/[scenario].json

Notes about hook behavior observed during testing:
- Bash tool with timeout exceeded does not generate PostToolUse hook invocation
- Interrupted Bash tool calls do not generate PostToolUse hook invocation
"""

from pathlib import Path

import pytest
import pytest_bazel

from claude_hooks.test_helpers import assert_tool_input_parsing, load_test_json
from claude_hooks.tool_models import BashInput, GlobInput, GrepInput, GrepOutputMode, LSInput, TaskInput


@pytest.mark.parametrize(
    ("tool_name", "scenario", "expected_tool_input"),
    [
        ("Bash", "command_only", BashInput(command="pwd", description=None, timeout=None)),
        ("Bash", "with_timeout_and_description", BashInput(command="sleep 5", timeout=10000, description="Test sleep")),
        ("Bash", "with_timeout_no_description", BashInput(command="sleep 2", timeout=5000, description=None)),
        ("Task", "basic", TaskInput(description="Test task", prompt="Return 42")),
        (
            "TodoWrite",
            "basic",
            {
                "todos": [
                    {"content": "Task A", "status": "completed", "priority": "low", "id": "todo3"},
                    {"content": "Task B", "status": "in_progress", "priority": "medium", "id": "todo2"},
                    {"content": "Task C", "status": "pending", "priority": "high", "id": "todo1"},
                ]
            },
        ),
        ("Glob", "pattern_only", GlobInput(pattern="*.md", path=None)),
        ("Glob", "with_path", GlobInput(pattern="*.toml", path=Path("/Users/user/test/personal"))),
        (
            "Grep",
            "pattern_only",
            GrepInput(pattern="hello\\.py", path=None, glob=None, output_mode=GrepOutputMode.FILES_WITH_MATCHES),
        ),
        ("WebFetch", "basic", {"url": "https://httpbin.org/json", "prompt": "Parse JSON"}),
        ("LS", "with_ignore_array", LSInput(path=Path("/Users/user/test"), ignore=["*.md", "node_modules", "target"])),
        ("LS", "detailed_response", LSInput(path=Path("/Users/user/test/docs"), ignore=["*.tmp", "*.cache"])),
        ("exit_plan_mode", "basic", {"plan": "Demo complete"}),
        (
            "mcp_multiedit_lookalike",
            "basic",
            {"file_path": "/path/to/file.txt", "edits": [{"old_string": "old", "new_string": "new"}]},
        ),
    ],
)
def test_tool_input_parsing(tool_name, scenario, expected_tool_input):
    """Test that tool JSON is parsed correctly with proper types."""
    raw_json = load_test_json(tool_name, scenario)
    assert_tool_input_parsing(raw_json, expected_tool_input, f"{tool_name}/{scenario}")


if __name__ == "__main__":
    pytest_bazel.main()
