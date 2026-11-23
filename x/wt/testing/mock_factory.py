"""Mock factory to reduce fixture proliferation and provide flexible test doubles."""

from typing import Any
from unittest.mock import Mock

from x.wt.server.git_manager import GitManager
from x.wt.server.github_client import GitHubInterface
from x.wt.server.worktree_service import WorktreeService
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


class ServiceBuilder:
    def __init__(self, test_config):
        self.config = test_config
        self.mocks = {}
        self._use_real = set()

    def with_real_git(self):
        self._use_real.add("git")
        return self

    def with_mock_github(self, **behaviors):
        self.mocks["github"] = MockFactory.github_client(**behaviors)
        return self

    def build_worktree_service(self):
        # Create real instances for components marked as 'real'
        git_manager = GitManager(config=self.config) if "git" in self._use_real else MockFactory.git_manager()

        github_client = self.mocks.get("github", MockFactory.github_client())

        return WorktreeService(git_manager, github_client)
