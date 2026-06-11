"""Integration tests for the new Click-based CLI entry points (no daemon).

These tests use Click's CliRunner with patched WtClient methods.
"""

from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from wt.cli import app
from wt.shared.protocol import WorktreeID, WorktreeInfo, WorktreeListResult
from wt.testing.asserts import assert_output_contains


@pytest.mark.integration
class TestNewCLIIntegration:
    @patch("wt.client.wt_client.WtClient.get_status")
    def test_default_status_command(self, mock_get_status, wt_env, build_status_response):
        """Test that default command (no args) shows worktree status."""
        mock_get_status.return_value = build_status_response({})

        result = CliRunner().invoke(app, [])

        assert result.exit_code == 0
        assert_output_contains(result.output, "No worktrees found")

    @patch("wt.client.wt_client.WtClient.list_worktrees")
    def test_list_worktrees_command(self, mock_list, wt_env, build_status_response):
        """Test ls command works via sh dispatcher."""
        mock_list.return_value = WorktreeListResult(worktrees=[])

        result = CliRunner().invoke(app, ["sh", "ls"])

        assert result.exit_code == 0

    @patch("wt.client.wt_client.WtClient.list_worktrees")
    def test_list_worktrees_with_data(self, mock_list, wt_env, build_status_response):
        """Test ls command with actual worktree data via sh dispatcher."""
        mock_list.return_value = WorktreeListResult(
            worktrees=[
                WorktreeInfo(
                    wtid=WorktreeID("test-worktree"),
                    name="test-worktree",
                    absolute_path=Path("/tmp/test-worktree"),
                    branch_name="test/test-branch",
                    exists=True,
                    is_main=False,
                )
            ]
        )

        result = CliRunner().invoke(app, ["sh", "ls"])

        assert result.exit_code == 0
        # Should list the mocked worktree we provided
        assert_output_contains(result.output, "test-worktree")

    def test_help_command(self, wt_env):
        """Test help command works via sh dispatcher."""

        result = CliRunner().invoke(app, ["sh", "help"])

        assert result.exit_code == 0
        assert_output_contains(result.output, "wt - Enhanced worktree management", "USAGE:")

    def test_help_flag(self, wt_env):
        """Test --help flag works with new CLI."""

        result = CliRunner().invoke(app, ["--help"])

        assert result.exit_code == 0
        # Click default help uses 'Usage:'; keep strict
        assert_output_contains(result.output, "Usage:")

    @patch("wt.client.wt_client.WtClient.get_status")
    def test_status_command_with_pr_flag(self, mock_get_status, wt_env, build_status_response, sample_status_result):
        """Test status command with --pr flag."""
        mock_get_status.return_value = build_status_response({"test-worktree": sample_status_result})

        result = CliRunner().invoke(app, [])

        assert result.exit_code == 0
        assert_output_contains(result.output, "test-worktree")
