"""Unit tests for status_handler."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock, Mock

import pygit2
import pytest
import pytest_bazel
from more_itertools import one

from x.wt.server.handlers.status_handler import get_status
from x.wt.server.rpc import ServiceDependencies
from x.wt.server.worktree_index import WorktreeIndex
from x.wt.shared.protocol import (
    Collector,
    ComponentState,
    GitstatusdData,
    GitstatusdState,
    StatusParams,
    StatusResultError,
    StatusResultOk,
)


@pytest.fixture
def status_deps(repo_factory, config_factory) -> ServiceDependencies:
    """Create ServiceDependencies with mocked services for status handler tests."""
    repo_path = repo_factory.create_repo()
    factory = config_factory(repo_path)
    config = factory.minimal()

    git_refs_watcher = Mock()
    git_refs_watcher.ahead_behind_cache.return_value = {}

    git_manager = Mock()

    return ServiceDependencies(
        config=config,
        git_manager=git_manager,
        index=WorktreeIndex.build([], config.main_repo),
        run_discovery_once=AsyncMock(),
        gitstatusd_client=Mock(return_value=None),
        github_watcher=None,
        git_refs_watcher=git_refs_watcher,
        discovery_scanning=Mock(return_value=False),
        coordinator=Mock(),
        worktree_service=Mock(),
    )


async def test_get_status_returns_error_on_git_failure(status_deps: ServiceDependencies):
    """When git_manager.get_repo raises, the worktree should return StatusResultError."""
    status_deps.git_manager.get_repo.side_effect = pygit2.GitError("repository not found")  # type: ignore[attr-defined]

    response = await get_status(status_deps, StatusParams())

    item = one(response.items.values())

    assert isinstance(item.result, StatusResultError)
    assert "repository not found" in item.result.error


async def test_get_status_returns_ok_on_success(status_deps: ServiceDependencies):
    """When git operations succeed, the worktree should return StatusResultOk."""
    mock_repo = Mock()
    mock_repo.head_is_detached = False
    mock_repo.head.shorthand = "feature-branch"
    status_deps.git_manager.get_repo.return_value = mock_repo  # type: ignore[attr-defined]
    status_deps.git_manager.get_commit_info.return_value = None  # type: ignore[attr-defined]

    response = await get_status(status_deps, StatusParams())

    item = one(response.items.values())

    assert isinstance(item.result, StatusResultOk)
    assert item.result.status.branch_name == "feature-branch"


async def test_empty_index_schedules_discovery_and_reports_main_worktree(status_deps: ServiceDependencies):
    discovery_started = asyncio.Event()

    async def discover() -> None:
        discovery_started.set()

    git_manager = Mock()
    git_manager.get_repo.return_value.head_is_detached = True
    git_manager.get_commit_info.return_value = None
    deps = replace(
        status_deps,
        index=WorktreeIndex(by_path={}, by_name={}, main=None),
        run_discovery_once=discover,
        git_manager=git_manager,
    )

    response = await get_status(deps, StatusParams())
    await discovery_started.wait()

    item = one(response.items.values())
    assert item.absolute_path == deps.config.main_repo
    assert isinstance(item.result, StatusResultOk)


async def test_get_status_reads_gitstatusd_client(status_deps: ServiceDependencies) -> None:
    mock_repo = Mock()
    mock_repo.head_is_detached = True
    status_deps.git_manager.get_repo.return_value = mock_repo  # type: ignore[attr-defined]
    status_deps.git_manager.get_commit_info.return_value = None  # type: ignore[attr-defined]
    client = Mock(is_running=True)
    client.status.return_value = Collector[GitstatusdData]().ok(
        GitstatusdData(
            branch=None,
            commit=None,
            staged=2,
            unstaged=1,
            untracked=4,
            conflicted=0,
            remote_ahead=None,
            remote_behind=None,
        )
    )
    deps = replace(status_deps, gitstatusd_client=lambda _path: client)

    response = await get_status(deps, StatusParams())

    result = one(response.items.values()).result
    assert isinstance(result, StatusResultOk)
    assert result.status.dirty_files_lower_bound == 3
    assert result.status.untracked_files_lower_bound == 4
    assert result.status.gitstatusd_state == GitstatusdState.RUNNING
    assert response.readiness_summary is not None
    assert response.readiness_summary.with_gitstatusd == 1


@pytest.mark.parametrize("scanning", [False, True])
async def test_get_status_reports_current_discovery_state(status_deps: ServiceDependencies, scanning: bool) -> None:
    deps = replace(status_deps, discovery_scanning=lambda: scanning)
    deps.git_manager.get_repo.return_value.head_is_detached = True  # type: ignore[attr-defined]
    deps.git_manager.get_commit_info.return_value = None  # type: ignore[attr-defined]

    response = await get_status(deps, StatusParams())

    assert response.readiness_summary is not None
    assert response.readiness_summary.discovery_scanning is scanning
    assert response.components is not None
    assert response.components.discovery.state == (ComponentState.SCANNING if scanning else ComponentState.OK)


if __name__ == "__main__":
    pytest_bazel.main()
