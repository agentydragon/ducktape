"""Integration tests for claude-linter-v2."""

import json
import subprocess

import pytest_bazel

from util.bazel.subprocess import run_python_module


def _has_ruff() -> bool:
    """Check if ruff CLI is available."""
    try:
        result = run_python_module("ruff", "--version", capture_output=True, check=False, timeout=5)
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _run_cli(*args: str, **kwargs) -> subprocess.CompletedProcess:
    """Run the claude-linter-v2 CLI via run_python_module."""
    return run_python_module("x.claude_linter_v2.cli", *args, capture_output=True, text=True, check=False, **kwargs)


class TestCLIIntegration:
    """Test the full CLI integration."""

    def test_pre_hook_clean_code(self, tmp_path):
        """Test that pre-hook passes clean code."""
        request_data = {
            "hook_event_name": "PreToolUse",
            "tool_name": "Write",
            "tool_input": {
                "file_path": str(tmp_path / "test_clean.py"),
                "content": """
def hello():
    try:
        print("Hello, world!")
    except ValueError as e:
        print(f"Error: {e}")
""",
            },
            "session_id": "12345678-1234-5678-1234-567812345680",
        }

        result = _run_cli("hook", input=json.dumps(request_data))

        assert result.returncode == 0
        response = json.loads(result.stdout)
        assert response["continue"] is True
        # Clean code should not be blocked
        assert response.get("decision") != "block"

    def test_pre_hook_invalid_json(self):
        """Test that pre-hook handles invalid JSON gracefully."""
        result = _run_cli("hook", input="not valid json")

        # Invalid JSON should crash the CLI
        assert result.returncode != 0
        assert "JSON parse error" in result.stderr

    def test_pre_hook_non_python_file(self, tmp_path):
        """Test that pre-hook passes non-Python files."""
        request_data = {
            "hook_event_name": "PreToolUse",
            "tool_name": "Write",
            "tool_input": {
                "file_path": str(tmp_path / "test.txt"),
                "content": "This is just a text file with except: and hasattr",
            },
            "session_id": "12345678-1234-5678-1234-567812345683",
        }

        result = _run_cli("hook", input=json.dumps(request_data))

        assert result.returncode == 0
        response = json.loads(result.stdout)
        assert response["continue"] is True
        # Non-Python files just return {"continue": true}

    def test_post_hook_basic(self, tmp_path):
        """Test that post-hook runs without errors."""
        request_data = {
            "hook_event_name": "PostToolUse",
            "tool_name": "Write",
            "tool_input": {"file_path": str(tmp_path / "test_post.py"), "content": "x=1+2  # poorly formatted"},
            "session_id": "12345678-1234-5678-1234-567812345684",
        }

        result = _run_cli("hook", input=json.dumps(request_data))

        # CLI always exits 0
        assert result.returncode == 0
        response = json.loads(result.stdout)
        assert response["continue"] is True
        # Post-hook may apply autofix (ruff formatting) even to clean code
        if response.get("decision") == "block":
            assert "Autofix:" in response["reason"]


class TestSessionCommands:
    """Test session management commands."""

    def test_session_list(self, tmp_path):
        """Test listing sessions."""
        result = run_python_module(
            "x.claude_linter_v2.cli", "session", "list", capture_output=True, text=True, cwd=tmp_path, check=False
        )

        assert result.returncode == 0
        # Isolated env has no sessions
        assert "No active sessions found" in result.stdout

    def test_session_allow(self, tmp_path):
        """Test adding an allow rule."""
        result = run_python_module(
            "x.claude_linter_v2.cli",
            "session",
            "allow",
            "Edit('**/*.py')",
            capture_output=True,
            text=True,
            cwd=tmp_path,
            check=False,
        )

        assert result.returncode == 0
        # Isolated env has no sessions to apply rule to
        assert "No active sessions found" in result.stdout


if __name__ == "__main__":
    pytest_bazel.main()
