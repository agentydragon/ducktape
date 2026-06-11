"""Mock factory to reduce fixture proliferation and provide flexible test doubles."""

from pathlib import Path
from typing import Any
from unittest.mock import Mock

from wt.client.view_formatter import ViewFormatter
from wt.client.wt_client import WtClient
from wt.server.git_manager import GitManager
from wt.server.github_client import GitHubInterface
from wt.server.worktree_service import WorktreeService
from wt.shared.protocol import DaemonHealth, DaemonHealthStatus, StatusResponse
from wt.testing.data import MockBehaviors


class MockFactory:
    """Factory for creating configured mocks with standard behaviors."""

    @staticmethod
    def github_client(
        *,
        pr_list_returns: list[dict[str, Any]] | None = None,
        pr_search_returns: list[dict[str, Any]] | None = None,
        pr_view_returns: dict[str, Any] | None = None,
        **kwargs,
    ) -> Mock:
        """Create a configured GitHub client mock."""
        mock = Mock(spec=GitHubInterface)

        # Set default behaviors
        mock.pr_list.return_value = pr_list_returns or MockBehaviors.GitHub.empty_pr_list()
        mock.pr_search.return_value = pr_search_returns or MockBehaviors.GitHub.empty_pr_list()
        mock.pr_view.return_value = pr_view_returns

        # Allow override of any method via kwargs
        for method_name, return_value in kwargs.items():
            if method_name in mock.__dir__():
                mock.configure_mock(**{f"{method_name}.return_value": return_value})

        return mock

    @staticmethod
    def git_manager(
        *,
        branches: list[str] | None = None,
        worktrees: list[str] | None = None,
        working_status: tuple[list[str], list[str]] | None = None,
        **kwargs,
    ) -> Mock:
        """Create a configured git manager mock."""
        mock = Mock(spec=GitManager)

        # Set default behaviors
        mock.list_branches.return_value = branches or MockBehaviors.Git.standard_branches()
        mock.list_worktrees.return_value = worktrees or []
        mock.get_working_directory_status.return_value = working_status or MockBehaviors.Git.clean_status()

        # Common git operations
        mock.worktree_add.return_value = None
        mock.worktree_remove.return_value = None
        mock.branch_exists.return_value = True

        # Allow override of any method via kwargs
        for method_name, return_value in kwargs.items():
            if method_name in mock.__dir__():
                mock.configure_mock(**{f"{method_name}.return_value": return_value})

        return mock

    @staticmethod
    def daemon_client(
        *, status_response: StatusResponse | None = None, get_status_returns: StatusResponse | None = None, **kwargs
    ) -> Mock:
        """Create a configured daemon client mock."""
        mock = Mock(spec=WtClient)

        # Default empty status response
        default_response = status_response or StatusResponse(
            items={},
            total_processing_time_ms=0.0,
            concurrent_requests=1,
            daemon_health=DaemonHealth(status=DaemonHealthStatus.OK),
        )

        mock.get_status.return_value = get_status_returns or default_response
        mock.create_worktree.return_value = Mock(success=True, absolute_path=Path("/test/path"))
        mock.delete_worktree.return_value = Mock(success=True)
        mock.list_worktrees.return_value = Mock(worktrees=[])

        # Allow override of any method via kwargs
        for method_name, return_value in kwargs.items():
            if method_name in mock.__dir__():
                mock.configure_mock(**{f"{method_name}.return_value": return_value})

        return mock

    @staticmethod
    def view_formatter(**kwargs) -> Mock:
        """Create a view formatter mock."""

        mock = Mock(spec=ViewFormatter)
        mock.render_worktree_status_all.return_value = None
        mock.render_single_status.return_value = None

        # Allow override of any method via kwargs
        for method_name, return_value in kwargs.items():
            if method_name in mock.__dir__():
                mock.configure_mock(**{f"{method_name}.return_value": return_value})

        return mock

    @staticmethod
    def process_info_list(*, running_processes: list[dict[str, Any]] | None = None) -> list[Mock]:
        """Create a list of process info mocks."""
        if running_processes is None:
            return []

        processes = []
        for proc_data in running_processes:
            mock_proc = Mock()
            mock_proc.pid = proc_data.get("pid", 1234)
            mock_proc.name = proc_data.get("name", "test-process")
            processes.append(mock_proc)

        return processes


class ServiceBuilder:
    """Builder pattern for creating service instances with configurable dependencies."""

    def __init__(self, test_config):
        """Initialize builder with test configuration."""
        self.config = test_config
        self.mocks = {}
        self._use_real = set()

    def with_real_git(self):
        """Use real GitManager instead of mock."""
        self._use_real.add("git")
        return self

    def with_mock_git(self, **behaviors):
        """Use mock GitManager with specified behaviors."""
        self.mocks["git"] = MockFactory.git_manager(**behaviors)
        return self

    def with_real_github(self):
        """Use real GitHub client (requires network)."""
        self._use_real.add("github")
        return self

    def with_mock_github(self, **behaviors):
        """Use mock GitHub client with specified behaviors."""
        self.mocks["github"] = MockFactory.github_client(**behaviors)
        return self

    def with_mock_daemon(self, **behaviors):
        """Use mock daemon client with specified behaviors."""
        self.mocks["daemon"] = MockFactory.daemon_client(**behaviors)
        return self

    def build_worktree_service(self):
        """Build WorktreeService with configured dependencies."""

        # Create real instances for components marked as 'real'
        if "git" in self._use_real:
            git_manager = GitManager(config=self.config)
        else:
            git_manager = self.mocks.get("git", MockFactory.git_manager())

        if "github" in self._use_real:
            github_client = GitHubInterface(self.config.github_repo)
        else:
            github_client = self.mocks.get("github", MockFactory.github_client())

        return WorktreeService(git_manager, github_client)

    def build_cli_dependencies(self):
        """Build CLI dependencies (config, formatter, daemon_client)."""
        formatter = MockFactory.view_formatter()
        daemon_client = self.mocks.get("daemon", MockFactory.daemon_client())

        return self.config, formatter, daemon_client
