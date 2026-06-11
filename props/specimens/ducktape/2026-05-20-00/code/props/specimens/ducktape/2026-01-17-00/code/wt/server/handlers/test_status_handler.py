"""Unit tests for status_handler."""

from __future__ import annotations

from unittest.mock import Mock

import pygit2
import pytest

from wt.server.handlers.status_handler import get_status
from wt.server.rpc import ServiceDependencies
from wt.shared.protocol import StatusParams, StatusResultError, StatusResultOk


@pytest.fixture
def status_deps(repo_factory, config_factory) -> ServiceDependencies:
    """Create ServiceDependencies with mocked services for status handler tests."""
    repo_path = repo_factory.create_repo()
    factory = config_factory(repo_path)
    config = factory.minimal()

    gitstatusd = Mock()
    gitstatusd.get_client.return_value = None

    git_refs_watcher = Mock()
    git_refs_watcher.ahead_behind_cache.return_value = {}

    git_manager = Mock()

    index = Mock()
    index.list_paths.return_value = [config.main_repo]

    discovery = Mock()
    discovery.is_scanning.return_value = False

    return ServiceDependencies(
        config=config,
        git_manager=git_manager,
        index=index,
        gitstatusd=gitstatusd,
        github_watcher=None,
        git_refs_watcher=git_refs_watcher,
        discovery=discovery,
        coordinator=Mock(),
    )


@pytest.mark.asyncio
async def test_get_status_returns_error_on_git_failure(status_deps: ServiceDependencies):
    """When git_manager.get_repo raises, the worktree should return StatusResultError."""
    status_deps.git_manager.get_repo.side_effect = pygit2.GitError("repository not found")  # type: ignore[attr-defined]

    response = await get_status(status_deps, StatusParams())

    assert len(response.items) == 1
    item = next(iter(response.items.values()))

    assert isinstance(item.result, StatusResultError)
    assert "repository not found" in item.result.error


@pytest.mark.asyncio
async def test_get_status_returns_ok_on_success(status_deps: ServiceDependencies):
    """When git operations succeed, the worktree should return StatusResultOk."""
    mock_repo = Mock()
    mock_repo.head_is_detached = False
    mock_repo.head.shorthand = "feature-branch"
    status_deps.git_manager.get_repo.return_value = mock_repo  # type: ignore[attr-defined]
    status_deps.git_manager.get_commit_info.return_value = None  # type: ignore[attr-defined]

    response = await get_status(status_deps, StatusParams())

    assert len(response.items) == 1
    item = next(iter(response.items.values()))

    assert isinstance(item.result, StatusResultOk)
    assert item.result.status.branch_name == "feature-branch"
