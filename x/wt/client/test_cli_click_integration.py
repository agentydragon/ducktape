"""Integration tests for the new Click-based CLI entry points (no daemon).

These tests use Click's CliRunner with patched WtClient methods.
"""

from pathlib import Path
from unittest.mock import patch

import pytest
import pytest_bazel
from typer.testing import CliRunner

from x.wt.cli import app
from x.wt.shared.constants import COMMAND_DESCRIPTIONS
from x.wt.shared.protocol import WorktreeID, WorktreeInfo, WorktreeListResult
from x.wt.testing.asserts import assert_output_contains


class TestNewCLIIntegration:
    @patch("x.wt.client.wt_client.WtClient.get_status")
    def test_default_status_command(self, mock_get_status, wt_env, build_status_response):
        """Test that default command (no args) shows worktree status."""
        mock_get_status.return_value = build_status_response({})

        result = CliRunner().invoke(app, [])

        assert result.exit_code == 0
        assert_output_contains(result.output, "No worktrees found")

    @patch("x.wt.client.wt_client.WtClient.list_worktrees")
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

    @pytest.mark.parametrize(("name", "description"), COMMAND_DESCRIPTIONS.items(), ids=list(COMMAND_DESCRIPTIONS))
    def test_help_command(self, wt_env, name, description):
        """`sh help` lists each reserved command as its own `wt <name>` row with its description.

        Matching the whole row keeps the FLAGS and `wt status [name]` rows, which repeat the
        `help` and `status` descriptions, from satisfying it.
        """

        result = CliRunner().invoke(app, ["sh", "help"])

        assert result.exit_code == 0
        assert ["wt", name, *description.split()] in [line.split() for line in result.output.splitlines()]


if __name__ == "__main__":
    pytest_bazel.main()
