"""Unit tests for the unified Claude linter binary."""

import pytest_bazel
from click.testing import CliRunner

from llm.claude_linter.cli import cli


def run_claude_linter(args: list[str], input_text: str | None = None):
    """Invoke the claude-linter CLI in-process and capture result."""
    runner = CliRunner()
    return runner.invoke(cli, args, input=input_text)


class TestUnifiedLinter:
    """Test cases for the unified linter entry point."""

    def test_hook_with_invalid_json(self):
        """Test that 'claude-linter hook' fails with invalid JSON."""
        result = run_claude_linter(["hook"], "invalid json")

        # Hook should fail with JSON decode error
        assert result.exit_code == 1
        assert "Invalid JSON input" in result.output

    def test_invalid_command(self):
        """Test that invalid command is rejected."""
        result = run_claude_linter(["invalid"])

        # Should fail with argument error
        assert result.exit_code == 2
        assert "No such command 'invalid'" in result.output

    def test_no_command(self):
        """Test that missing command requires a command."""
        result = run_claude_linter([])

        # No command -> error (CLI requires a subcommand)
        assert result.exit_code == 2
        assert "Usage:" in result.output

    def test_help(self):
        """Test that help text shows available commands."""
        result = run_claude_linter(["--help"])

        assert result.exit_code == 0
        assert "hook" in result.output
        assert "check" in result.output
        assert "clean" in result.output

    def test_debug_logs_not_created_by_default(self, tmp_path, monkeypatch):
        """Test that debug logs are not created by default."""
        # XDG_CACHE_HOME is set by the autouse fixture isolate_test_environment
        cache_dir = tmp_path / "claude-linter"

        # Ensure CLAUDE_LINTER_DEBUG is not set
        monkeypatch.delenv("CLAUDE_LINTER_DEBUG", raising=False)

        # Create a simple Python file to lint
        test_file = tmp_path / "test.py"
        test_file.write_text("x=1")  # This will trigger formatting issues

        # Run the check command
        run_claude_linter(["check", "--files", str(test_file)])

        # Check that no debug logs were created
        if cache_dir.exists():
            debug_logs = list(cache_dir.glob("debug-*.log"))
            assert len(debug_logs) == 0, f"Found unexpected debug logs: {debug_logs}"

    def test_debug_logs_created_when_enabled(self, tmp_path, monkeypatch):
        """Test that debug logs ARE created when CLAUDE_LINTER_DEBUG is set."""
        # XDG_CACHE_HOME is set by the autouse fixture isolate_test_environment
        cache_dir = tmp_path / "claude-linter"

        # Enable debug logging
        monkeypatch.setenv("CLAUDE_LINTER_DEBUG", "true")

        # Create a simple Python file to lint
        test_file = tmp_path / "test.py"
        test_file.write_text("x=1")  # This will trigger formatting issues

        # Run the check command
        run_claude_linter(["check", "--files", str(test_file)])

        # Check that debug logs WERE created
        assert cache_dir.exists(), "Cache directory should exist"
        debug_logs = list(cache_dir.glob("debug-*.log"))
        assert len(debug_logs) > 0, "Should have created at least one debug log"


if __name__ == "__main__":
    pytest_bazel.main()
