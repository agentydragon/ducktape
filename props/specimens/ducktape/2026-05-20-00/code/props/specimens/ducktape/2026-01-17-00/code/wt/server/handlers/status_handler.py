from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path

from ...shared.configuration import Configuration
from ...shared.env import is_test_mode
from ...shared.protocol import (
    BranchAheadBehind,
    CommitInfo,
    ComponentsStatus,
    ComponentState,
    ComponentStatus,
    DaemonHealth,
    DaemonHealthStatus,
    GitstatusdState,
    PRInfo,
    PRInfoDisabled,
    PRInfoOk,
    ReadinessSummary,
    StatusItem,
    StatusItemResult,
    StatusParams,
    StatusResponse,
    StatusResult,
    StatusResultError,
    StatusResultOk,
    WorktreeID,
)
from ..git_manager import GitManager, NoSuchRefError
from ..github_watcher import GitHubWatcher
from ..rpc import ServiceDependencies, rpc
from ..services import GitstatusdService
from ..worktree_ids import make_worktree_id, parse_worktree_id

logger = logging.getLogger(__name__)


def _get_commit_info(git_manager: GitManager, worktree_path: Path) -> CommitInfo | None:
    try:
        return git_manager.get_commit_info("HEAD", worktree_path)
    except NoSuchRefError:
        return None


def _get_ahead_behind(
    ahead_behind_data: dict[str, BranchAheadBehind], branch: str | None
) -> tuple[int | None, int | None]:
    if not branch:
        return (None, None)
    cached = ahead_behind_data.get(branch)
    if cached:
        return (cached.ahead, cached.behind)
    return (None, None)


def _log_task_exception(t: asyncio.Task) -> None:
    try:
        exc = t.exception()
        if exc:
            logger.exception("background task failed", exc_info=exc)
    except asyncio.CancelledError:
        pass
    except Exception:
        logger.exception("failed to inspect background task exception")


async def _compute_worktree_status(
    worktree_path: Path,
    *,
    git_manager: GitManager,
    gitstat: GitstatusdService,
    ahead_behind_data: dict[str, BranchAheadBehind],
    config: Configuration,
    github_watcher: GitHubWatcher | None,
) -> StatusItemResult:
    try:
        repo = git_manager.get_repo(worktree_path)
        branch_name = repo.head.shorthand if not repo.head_is_detached else None

        commit_info = _get_commit_info(git_manager, worktree_path)
        ahead, behind = _get_ahead_behind(ahead_behind_data, branch_name)

        gs_client = gitstat.get_client(worktree_path)
        worktree_last_error: str | None = None
        pr_info: PRInfo = PRInfoDisabled()

        if not gs_client:
            return StatusResultOk(
                status=StatusResult(
                    branch_name=branch_name or "",
                    dirty_files_lower_bound=0,
                    untracked_files_lower_bound=0,
                    last_updated_at=datetime.now(),
                    is_cached=False,
                    cache_age_ms=None,
                    is_stale=False,
                    commit_info=commit_info,
                    ahead_count=ahead,
                    behind_count=behind,
                    is_main=worktree_path.resolve() == config.main_repo.resolve(),
                    upstream_branch=config.upstream_branch,
                    pr_info=PRInfoDisabled(),
                    gitstatusd_state=GitstatusdState.STOPPED,
                    restarts=0,
                    last_error="gitstatusd client unavailable",
                )
            )

        collector = gs_client.status()
        last_ok = collector.last_ok
        if last_ok is not None:
            status_data = last_ok.value
            dirty_count = status_data.staged + status_data.unstaged
            untracked_count = status_data.untracked
            cache_age_ms = (time.time() - last_ok.at.timestamp()) * 1000
            last_updated_at = last_ok.at
        else:
            dirty_count, untracked_count = 0, 0
            cache_age_ms = None
            last_updated_at = datetime.now()
            task = asyncio.create_task(gs_client.update_working_status())
            task.add_done_callback(_log_task_exception)

        if collector.last_error is not None:
            worktree_last_error = collector.last_error.error

        # Derive pr_cache and pr_data from github_watcher
        pr_cache = github_watcher.pr_cache() if github_watcher else None
        pr_data = pr_cache.last_ok.value if pr_cache and pr_cache.last_ok else {}

        if pr_cache is None or pr_cache.last_ok is None:
            pr_info = PRInfoDisabled()
        elif branch_name:
            pr = pr_data.get(branch_name)
            pr_info = PRInfoOk(pr_data=pr) if pr is not None else PRInfoDisabled()

        # In WT_TEST_MODE, synchronously refresh once if PR cache not ready yet
        if isinstance(pr_info, PRInfoDisabled) and is_test_mode() and github_watcher and branch_name:
            await github_watcher.refresh_now()
            pr_cache_refreshed = github_watcher.pr_cache()
            if pr_cache_refreshed.last_ok:
                pr_refreshed = pr_cache_refreshed.last_ok.value.get(branch_name)
                if pr_refreshed is not None:
                    pr_info = PRInfoOk(pr_data=pr_refreshed)

        is_cached = last_ok is not None
        is_stale = bool(cache_age_ms and timedelta(milliseconds=cache_age_ms) > config.cache_refresh_age)
        state = GitstatusdState.RUNNING if gs_client.is_running else GitstatusdState.STOPPED

        return StatusResultOk(
            status=StatusResult(
                branch_name=branch_name or "",
                dirty_files_lower_bound=dirty_count,
                untracked_files_lower_bound=untracked_count,
                last_updated_at=last_updated_at,
                is_cached=is_cached,
                cache_age_ms=cache_age_ms,
                is_stale=is_stale,
                commit_info=commit_info,
                ahead_count=ahead,
                behind_count=behind,
                is_main=worktree_path.resolve() == config.main_repo.resolve(),
                upstream_branch=config.upstream_branch,
                pr_info=pr_info,
                gitstatusd_state=state,
                restarts=0,
                last_error=worktree_last_error,
            )
        )
    except Exception as e:
        logger.exception("Worktree processing failed: %s", worktree_path)
        return StatusResultError(error=str(e))


@rpc.method("get_status", params=StatusParams)
async def get_status(deps: ServiceDependencies, params: StatusParams) -> StatusResponse:
    gitstat = deps.gitstatusd
    github_watcher = deps.github_watcher
    git_refs_watcher = deps.git_refs_watcher
    git_manager = deps.git_manager
    index = deps.index
    discovery = deps.discovery
    config = deps.config
    worktree_ids = params.worktree_ids

    ahead_behind_data = git_refs_watcher.ahead_behind_cache()

    if worktree_ids:
        worktree_paths: list[Path] = []
        for wtid in worktree_ids:
            worktree_name = parse_worktree_id(wtid)
            worktree_path = config.worktrees_dir / worktree_name
            worktree_paths.append(worktree_path)
    else:
        if not index.list_paths():
            logger.debug("Index empty; scheduling discovery run")
            t = asyncio.create_task(index.ensure_discovery())
            t.add_done_callback(_log_task_exception)
        worktree_paths = index.list_paths()
        if not worktree_paths:
            worktree_paths = [config.main_repo]

    items: dict[WorktreeID, StatusItem] = {}

    async def process_single_worktree(worktree_path: Path) -> StatusItem:
        single_start = time.perf_counter()
        result = await _compute_worktree_status(
            worktree_path,
            git_manager=git_manager,
            gitstat=gitstat,
            ahead_behind_data=ahead_behind_data,
            config=config,
            github_watcher=github_watcher,
        )
        processing_time_ms = (time.perf_counter() - single_start) * 1000
        return StatusItem(
            name=worktree_path.name, absolute_path=worktree_path, processing_time_ms=processing_time_ms, result=result
        )

    gather_start = time.perf_counter()
    status_items = await asyncio.gather(*[process_single_worktree(p) for p in worktree_paths])
    total_time = (time.perf_counter() - gather_start) * 1000

    for item in status_items:
        items[make_worktree_id(item.name)] = item

    total_wt = len(worktree_paths)
    with_git = sum(1 for p in (gitstat.get_client(pth) for pth in worktree_paths) if p and p.is_running)

    any_wt_error = any(
        not isinstance(it.result, StatusResultOk) or it.result.status.last_error is not None for it in items.values()
    )
    # Derive github_state from the centralized watcher's pr_cache
    github_state = ComponentState.DISABLED
    if config.github_enabled and github_watcher:
        pr_cache_state = github_watcher.pr_cache()
        if pr_cache_state.last_ok is not None:
            github_state = ComponentState.OK
        elif pr_cache_state.last_error is not None:
            github_state = ComponentState.ERROR
        else:
            github_state = ComponentState.STARTING
    readiness = ReadinessSummary(
        total_worktrees=total_wt,
        with_gitstatusd=with_git,
        discovery_scanning=discovery.is_scanning(),
        github=github_state,
    )

    components = ComponentsStatus(
        discovery=ComponentStatus(state=(ComponentState.SCANNING if discovery.is_scanning() else ComponentState.OK)),
        github=ComponentStatus(state=github_state),
        gitstatusd=ComponentStatus(
            state=(
                ComponentState.OK
                if (with_git == total_wt and total_wt > 0 and not any_wt_error)
                else ComponentState.ERROR
            ),
            metrics={"running": with_git, "total": total_wt},
        ),
    )

    return StatusResponse(
        items=items,
        total_processing_time_ms=total_time,
        concurrent_requests=len(worktree_paths),
        daemon_health=DaemonHealth(status=DaemonHealthStatus.OK),
        readiness_summary=readiness,
        components=components,
    )
