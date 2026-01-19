"""Integration tests that verify actual CLI output formatting."""

from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest
import pytest_bazel

from wt.cli import app
from wt.shared.protocol import CommitInfo, StatusResult
from wt.testing.asserts import assert_output_contains


@pytest.fixture
def cli_runner_with_env(cli_runner, wt_env):
    """Factory fixture for running CLI with mocked environment."""

    def _run_with_mocked_status(status_response, cli_args, mock_get_status):
        """Run CLI with mocked status response and proper environment setup."""
        mock_get_status.return_value = status_response
        return cli_runner.invoke(app, cli_args)

    return _run_with_mocked_status


@pytest.mark.integration
class TestCLIOutputFormat:
    @patch("wt.client.wt_client.WtClient.get_status")
    def test_status_table_rendering(self, mock_get_status, cli_runner_with_env, build_status_response):
        """Test that the status table renders correctly with real formatting."""
        # Create status data
        commit_info = CommitInfo(
            hash="abcdef1234567890abcdef1234567890abcdef12",
            short_hash="abcdef12",
            message="Add new feature",
            author="Test Author",
            date="2024-01-15T10:30:00",
        )

        # Create test results
        results = {
            "main": (
                StatusResult(
                    branch_name="master",
                    dirty_files_lower_bound=1,
                    untracked_files_lower_bound=1,
                    last_updated_at=datetime.now(),
                    commit_info=commit_info,
                    ahead_count=2,
                    behind_count=0,
                    is_main=True,
                    upstream_branch="master",
                ),
                Path("/test/main"),
            ),
            "feature-branch": (
                StatusResult(
                    branch_name="feature/test",
                    dirty_files_lower_bound=0,
                    untracked_files_lower_bound=0,
                    last_updated_at=datetime.now(),
                    commit_info=commit_info,
                    ahead_count=1,
                    behind_count=0,
                    is_main=False,
                    upstream_branch="master",
                ),
                Path("/test/feature-branch"),
            ),
        }

        status_response = build_status_response(results)
        result = cli_runner_with_env(status_response, [], mock_get_status)

        assert result.exit_code == 0
        output = result.output

        # Verify content appears in output
        assert_output_contains(output, "main", "feature-branch")

    @patch("wt.client.wt_client.WtClient.get_status")
    def test_status_unknown_when_not_cached(self, mock_get_status, cli_runner_with_env, build_status_response):
        """When status isn't cached yet, show 'unknown' instead of 'clean'."""
        commit_info = CommitInfo(
            hash="abcdef1234567890abcdef1234567890abcdef12",
            short_hash="abcdef12",
            message="Init",
            author="Test",
            date="2024-01-15T10:30:00",
        )
        results = {
            "test1": (
                StatusResult(
                    branch_name="test/test1",
                    dirty_files_lower_bound=0,
                    untracked_files_lower_bound=0,
                    last_updated_at=datetime.now(),
                    commit_info=commit_info,
                    ahead_count=0,
                    behind_count=0,
                    is_main=False,
                    upstream_branch="master",
                    is_cached=False,
                ),
                Path("/test/test1"),
            )
        }
        status_response = build_status_response(results)
        result = cli_runner_with_env(status_response, [], mock_get_status)
        assert result.exit_code == 0
        assert_output_contains(result.output, "unknown")


if __name__ == "__main__":
    pytest_bazel.main()
