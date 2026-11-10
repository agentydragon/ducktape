"""Mock factory to reduce fixture proliferation and provide flexible test doubles."""

from pathlib import Path
from typing import Any
from unittest.mock import Mock

from x.wt.client.view_formatter import ViewFormatter
from x.wt.client.wt_client import WtClient
from x.wt.server.git_manager import GitManager
from x.wt.server.github_client import GitHubInterface
from x.wt.server.worktree_service import WorktreeService
from x.wt.shared.protocol import DaemonHealth, DaemonHealthStatus, StatusResponse
from x.wt.testing.data import MockBehaviors


def _configure_return_values(mock: Mock, overrides: dict[str, Any]) -> Mock:
    for method_name, return_value in overrides.items():
        if method_name in mock.__dir__():
            mock.configure_mock(**{f"{method_name}.return_value": return_value})
    return mock


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
        mock = Mock(spec=GitHubInterface)
        mock.pr_list.return_value = pr_list_returns or MockBehaviors.GitHub.empty_pr_list()
        mock.pr_search.return_value = pr_search_returns or MockBehaviors.GitHub.empty_pr_list()
        mock.pr_view.return_value = pr_view_returns
        return _configure_return_values(mock, kwargs)

    @staticmethod
    def git_manager(
        *,
        branches: list[str] | None = None,
        worktrees: list[str] | None = None,
        working_status: tuple[list[str], list[str]] | None = None,
        **kwargs,
    ) -> Mock:
        mock = Mock(spec=GitManager)
        mock.list_branches.return_value = branches or MockBehaviors.Git.standard_branches()
        mock.list_worktrees.return_value = worktrees or []
        mock.get_working_directory_status.return_value = working_status or MockBehaviors.Git.clean_status()

        # Common git operations
        mock.worktree_add.return_value = None
        mock.worktree_remove.return_value = None
        mock.branch_exists.return_value = True

        return _configure_return_values(mock, kwargs)

    @staticmethod
    def daemon_client(
        *, status_response: StatusResponse | None = None, get_status_returns: StatusResponse | None = None, **kwargs
    ) -> Mock:
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

        return _configure_return_values(mock, kwargs)

    @staticmethod
    def view_formatter(**kwargs) -> Mock:
        mock = Mock(spec=ViewFormatter)
        mock.render_worktree_status_all.return_value = None
        mock.render_single_status.return_value = None
        return _configure_return_values(mock, kwargs)

    @staticmethod
    def process_info_list(*, running_processes: list[dict[str, Any]] | None = None) -> list[Mock]:
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
    def __init__(self, test_config):
        self.config = test_config
        self.mocks = {}
        self._use_real = set()

    def with_real_git(self):
        self._use_real.add("git")
        return self

    def with_mock_git(self, **behaviors):
        self.mocks["git"] = MockFactory.git_manager(**behaviors)
        return self

    def with_real_github(self):
        """Use real GitHub client (requires network)."""
        self._use_real.add("github")
        return self

    def with_mock_github(self, **behaviors):
        self.mocks["github"] = MockFactory.github_client(**behaviors)
        return self

    def with_mock_daemon(self, **behaviors):
        self.mocks["daemon"] = MockFactory.daemon_client(**behaviors)
        return self

    def build_worktree_service(self):
        # Create real instances for components marked as 'real'
        if "git" in self._use_real:
            git_manager = GitManager(config=self.config)
        else:
            git_manager = self.mocks.get("git", MockFactory.git_manager())

        if "github" in self._use_real:
            if self.config.github_repo is None:
                raise ValueError("use_real('github') requires a configured github_repo")
            github_client = GitHubInterface(self.config.github_repo)
        else:
            github_client = self.mocks.get("github", MockFactory.github_client())

        return WorktreeService(git_manager, github_client)

    def build_cli_dependencies(self):
        formatter = MockFactory.view_formatter()
        daemon_client = self.mocks.get("daemon", MockFactory.daemon_client())

        return self.config, formatter, daemon_client
