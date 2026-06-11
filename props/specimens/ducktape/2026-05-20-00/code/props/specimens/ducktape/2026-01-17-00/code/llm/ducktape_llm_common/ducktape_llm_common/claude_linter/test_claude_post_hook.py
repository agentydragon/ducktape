#!/usr/bin/env python3
"""Unit tests for Claude Code post-tool-use hook."""

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import CliRunner

from ducktape_llm_common.claude_linter.cli import cli


def run_post_hook(test_input: dict[str, Any]):
    """Invoke post-hook CLI in-process and return the result."""
    runner = CliRunner()
    # Unified hook command - add hook_event_name to payload
    test_input["hook_event_name"] = "PostToolUse"
    test_input.setdefault("session_id", "12345678-1234-5678-1234-567812345678")
    payload = json.dumps(test_input)
    return runner.invoke(cli, ["hook"], input=payload)


def create_write_response(file_path: str | Path, content: str) -> dict[str, Any]:
    """Create a standard Write tool input/response structure for post-hook."""
    # ensure file exists with given content for post-hook to process
    path = Path(file_path)
    path.write_text(content)
    file_path = str(path)
    return {
        "tool_name": "Write",
        "tool_input": {"file_path": file_path, "content": content},
        "tool_response": {"type": "create", "filePath": file_path},
    }


@pytest.fixture
def pre_commit_config_fixing(chdir_tmp_path: Path):
    """Creates a fixing hook config with a dummy linter."""
    linter = chdir_tmp_path / "fixing_linter.py"
    linter.write_text(
        """#!/usr/bin/env python
import sys
for f in sys.argv[1:]:
    c=open(f).read()
    if 'fix-me' in c: open(f,'w').write(c.replace('fix-me','fixed')); sys.exit(1)
sys.exit(0)
"""
    )
    linter.chmod(0o755)
    config = {
        "repos": [
            {
                "repo": "local",
                "hooks": [
                    {"id": "fixing", "name": "fx", "entry": str(linter), "language": "script", "types": ["python"]}
                ],
            }
        ]
    }
    cfg = chdir_tmp_path / ".pre-commit-config.yaml"
    cfg.write_text(yaml.dump(config))
    return cfg


class TestPostHook:
    """Test cases for the post-hook."""

    def test_fixes_violations(self, tmp_path, pre_commit_config_fixing):
        """Test that post-hook auto-fixes violations."""
        content = """# This code has fix-me in it
def process(value):
    # fix-me: this needs fixing
    return value
"""
        # Create a temporary file with fixable violations
        test_file = tmp_path / "test.py"
        test_file.write_text(content)

        result = run_post_hook(create_write_response(test_file, content))

        # Should exit with code 0 (success)
        assert result.exit_code == 0

        # Check that file was modified
        fixed_content = test_file.read_text()

        # Should have fixed "fix-me" to "fixed"
        assert "fixed" in fixed_content
        assert "fix-me" not in fixed_content

        # Should show what was fixed (output includes JSON response)
        assert "FYI: Auto-fixes were applied" in result.output

    def test_reports_fixes(self, tmp_path, pre_commit_config_fixing):
        """Test that post-hook reports fixes to stderr for Claude to see."""
        content = """# fix-me: needs attention
def foo(x):
    return str(x)
"""
        test_file = tmp_path / "test.py"
        test_file.write_text(content)

        result = run_post_hook(create_write_response(test_file, content))

        assert result.exit_code == 0
        assert "FYI: Auto-fixes were applied" in result.output

    def test_handles_clean_files(self, tmp_path, pre_commit_config_fixing):
        """Test that post-hook handles files without violations."""
        content = """def add(a: int, b: int) -> int:
    return a + b
"""
        test_file = tmp_path / "test.py"
        test_file.write_text(content)

        result = run_post_hook(create_write_response(test_file, content))

        assert result.exit_code == 0
        # When no fixes needed, response should indicate continuation is allowed
        assert '"continue":true' in result.output

    def test_ignores_non_python(self, tmp_path):
        """Test that post-hook ignores non-Python files."""
        test_input = create_write_response(tmp_path / "test.txt", "Not Python")
        result = run_post_hook(test_input)
        assert result.exit_code == 0

    def test_handles_edit_tool(self, tmp_path, pre_commit_config_fixing):
        """Test that post-hook handles Edit tool."""
        content = """# fix-me: this needs attention
def foo(x):
    return str(x)
"""
        test_file = tmp_path / "test.py"
        test_file.write_text(content)

        test_input = {
            "tool_name": "Edit",
            "tool_input": {"file_path": str(test_file), "old_string": "foo", "new_string": "bar"},
        }
        result = run_post_hook(test_input)

        assert result.exit_code == 0
        # Check that file was auto-fixed
        fixed_content = test_file.read_text()
        assert "fixed" in fixed_content
        assert "fix-me" not in fixed_content

    def test_ignores_other_tools(self):
        """Test that post-hook ignores non-file-editing tools."""
        test_input = {"tool_name": "Read", "tool_input": {"file_path": "/some/file.py"}}
        result = run_post_hook(test_input)
        assert result.exit_code == 0

    def test_formats_code(self, tmp_path, pre_commit_config_fixing):
        """Test that post-hook runs the linter."""
        # Code with fix-me
        content = """def foo(x, y):
    # fix-me: add return type
    return x + y
"""
        test_file = tmp_path / "test.py"
        test_file.write_text(content)

        result = run_post_hook(create_write_response(test_file, content))

        assert result.exit_code == 0

        # Check fix was applied
        formatted_content = test_file.read_text()

        # Should have fixed "fix-me" to "fixed"
        assert "fixed" in formatted_content
        assert "fix-me" not in formatted_content
