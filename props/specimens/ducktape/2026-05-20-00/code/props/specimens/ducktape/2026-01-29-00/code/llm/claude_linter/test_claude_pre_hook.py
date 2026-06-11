#!/usr/bin/env python3
"""Unit tests for Claude Code pre-tool-use hook."""

import json
from pathlib import Path

import pytest_bazel
from click.testing import CliRunner

from llm.claude_linter.cli import cli
from llm.claude_linter.precommit_runner import PreCommitRunner


def run_pre_hook(test_input: str):
    """Invoke pre-hook CLI in-process and return the result."""
    runner = CliRunner()
    # Unified hook command - hook_event_name is in JSON payload
    return runner.invoke(cli, ["hook"], input=test_input)


def create_write_input(file_path: str | Path, content: str) -> str:
    """Create a standard Write tool input structure for PreToolUse."""
    return json.dumps(
        {
            "hook_event_name": "PreToolUse",
            "session_id": "12345678-1234-5678-1234-567812345678",
            "tool_name": "Write",
            "tool_input": {"file_path": str(file_path), "content": content},
        }
    )


PYTHON_MYPY_NONFIXABLE = """import requests

def fetch_data(url):
    response = requests.get(url)  # S113 - missing timeout
    return response.text
"""

PYTHON_MYPY_AUTOFIXABLE = """from typing import Union, Optional

def process(value: Union[str, int]) -> Optional[str]:
    if value:
        return str(value)
    else:
        return None
"""

PYTHON_OK = """def add(a: int, b: int) -> int:
    return a + b
"""


class TestPreHook:
    """Test cases for the pre-hook."""

    def test_blocks_non_fixable_violations(self, tmp_path, monkeypatch):
        """Test that pre-hook blocks files with non-fixable violations like S113."""
        # Simulate a file that pre-commit keeps trying to change

        call_count = 0
        original_content = PYTHON_MYPY_NONFIXABLE

        def mock_run(self, paths, cwd=None):
            nonlocal call_count
            call_count += 1

            # Both calls keep changing the file (simulating non-fixable issues)
            Path(paths[0]).write_text(original_content + f"\n# Changed by run {call_count}")
            return (2, "S113 violation", "timeout missing")

        monkeypatch.setattr(PreCommitRunner, "run", mock_run)
        result = run_pre_hook(create_write_input(tmp_path / "test.py", original_content))

        # Should exit with code 0 (we use JSON output)
        assert result.exit_code == 0
        assert call_count == 2  # Verify both passes ran
        # Parse JSON output and check fields
        output = json.loads(result.output)
        assert output["decision"] == "block"
        assert "non-fixable errors" in output["reason"].lower()
        assert "S113 violation" in output["reason"]
        assert "timeout missing" in output["reason"].lower()

    def test_allows_clean_files(self, tmp_path, monkeypatch):
        """Test that pre-hook allows files without violations."""
        # simulate clean file
        monkeypatch.setattr(PreCommitRunner, "run", lambda self, paths, cwd=None: (0, "", ""))
        result = run_pre_hook(create_write_input(tmp_path / "test.py", PYTHON_OK))

        assert result.exit_code == 0
        output = json.loads(result.output)
        # New API format - continue:true means allowed
        assert output.get("continue") is True

    def test_allows_auto_fixable_only(self, tmp_path, monkeypatch):
        """Test that pre-hook allows files with only auto-fixable violations."""
        # We need to simulate the two-pass behavior:
        # 1. First call with fix=True changes the content
        # 2. Second call with fix=True shows no more changes needed

        call_count = 0
        original_content = PYTHON_MYPY_AUTOFIXABLE
        fixed_content = PYTHON_MYPY_AUTOFIXABLE.replace("Union[str, int]", "str | int")

        def mock_run(self, paths, cwd=None):
            nonlocal call_count
            call_count += 1

            if call_count == 1:
                # First call: simulate fixing the file
                Path(paths[0]).write_text(fixed_content)
                return (0, "Fixed violations", "")
            # Second call: no more changes needed (content stable)
            # Don't change the file - it's already fixed
            return (0, "", "")

        monkeypatch.setattr(PreCommitRunner, "run", mock_run)
        result = run_pre_hook(create_write_input(tmp_path / "test.py", original_content))

        assert result.exit_code == 0
        assert call_count == 2  # Verify both passes ran
        output = json.loads(result.output)
        # New API format - continue:true means allowed
        assert output.get("continue") is True

    def test_ignores_non_python_files(self, tmp_path, monkeypatch):
        """Test that pre-hook ignores non-Python files."""
        # simulate ignore for non-python
        monkeypatch.setattr(PreCommitRunner, "run", lambda self, paths, cwd=None: (0, "", ""))
        result = run_pre_hook(create_write_input(file_path=tmp_path / "test.txt", content="This is not Python code"))
        assert result.exit_code == 0

    def test_ignores_other_tools(self, tmp_path, monkeypatch):
        """Test that pre-hook ignores non-Write tools."""
        # simulate ignore for other tools
        monkeypatch.setattr(PreCommitRunner, "run", lambda self, paths, cwd=None: (0, "", ""))
        result = run_pre_hook(
            json.dumps(
                {
                    "hook_event_name": "PreToolUse",
                    "session_id": "12345678-1234-5678-1234-567812345678",
                    "tool_name": "Edit",
                    "tool_input": {"file_path": str(tmp_path / "test.py"), "old_string": "foo", "new_string": "bar"},
                }
            )
        )
        assert result.exit_code == 0

    def test_invalid_json(self):
        """Test that pre-hook handles invalid JSON gracefully."""
        result = run_pre_hook("not valid json")
        assert result.exit_code == 1
        assert "Invalid JSON input" in result.output


if __name__ == "__main__":
    pytest_bazel.main()
