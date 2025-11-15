from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import logging
from pathlib import Path
import time

from ...shared.env import is_test_mode
from ...shared.protocol import (
    CommitInfo,
    ComponentsStatus,
    ComponentState,
    ComponentStatus,
    GitstatusdState,
    PRInfo,
    PRInfoDisabled,
    ReadinessSummary,
    StatusItem,
    StatusParams,
    StatusResponse,
    StatusResult,
    WorktreeID,
)
from ..rpc import ServiceDependencies, rpc
from ..worktree_ids import make_worktree_id, parse_worktree_id

logger = logging.getLogger(__name__)
_bg_tasks: set[asyncio.Task] = set()


def _log_task_done(t: asyncio.Task) -> None:
    try:
        exc = t.exception()
        if exc:
            logger.exception("background task failed", exc_info=exc)
    except Exception:
        logger.exception("failed to inspect background task exception")
    finally:
        _bg_tasks.discard(t)


@rpc.method("get_status", params=StatusParams)
async def get_status(deps: ServiceDependencies, params: StatusParams) -> StatusResponse:
    status = deps.status
    gitstat = deps.gitstatusd
    prs = deps.prs
    index = deps.index
    discovery = deps.discovery
    health = deps.health
    config = deps.config
    worktree_ids = params.worktree_ids

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
            _bg_tasks.add(t)
            t.add_done_callback(_log_task_done)
        worktree_paths = index.list_paths()
        if not worktree_paths:
            # Minimal safe fallback: include main repo to avoid empty UI when daemon just started
            worktree_paths = [config.main_repo]

    items: dict[WorktreeID, StatusItem] = {}

    async def process_single_worktree(worktree_path: Path):
        single_start = time.perf_counter()
        gs_client = gitstat.get_client(worktree_path)
        worktree_last_error: str | None = None
        meta = status
        pr_info: PRInfo = PRInfoDisabled()

        def _compute_status(path: Path):
            return (*meta.summarize_status(path), None)

        if not gs_client:
            single_time = (time.perf_counter() - single_start) * 1000
            state = GitstatusdState.STOPPED
            dirty_count, untracked_count = 0, 0
            # Surface explicit error to avoid silent downgrade
            commit_info_data, ahead_behind, branch_name, _ = _compute_status(worktree_path)
            last_updated_at = datetime.now()
            pr_info = PRInfoDisabled()
            is_cached = False
            cache_age_ms = None
            is_stale = False
            commit_info = CommitInfo.model_validate(commit_info_data) if commit_info_data else None
            wtid = make_worktree_id(worktree_path.name)
            return (
                wtid,
                StatusResult(
                    wtid=wtid,
                    name=worktree_path.name,
                    absolute_path=worktree_path,
                    branch_name=branch_name,
                    dirty_files_lower_bound=dirty_count,
                    untracked_files_lower_bound=untracked_count,
                    processing_time_ms=single_time,
                    last_updated_at=last_updated_at,
                    is_cached=is_cached,
                    cache_age_ms=cache_age_ms,
                    is_stale=is_stale,
                    commit_info=commit_info,
                    ahead_count=ahead_behind[0],
                    behind_count=ahead_behind[1],
                    is_main=worktree_path.resolve() == config.main_repo.resolve(),
                    upstream_branch=config.upstream_branch,
                    pr_info=pr_info,
                    gitstatusd_state=state,
                    restarts=0,
                    last_error="gitstatusd client unavailable",
                ),
                single_time,
            )
        try:
            summary = gs_client.get_cached_working_status()
            dirty_count = summary.dirty_lower_bound or 0
            untracked_count = summary.untracked_lower_bound or 0
            cache_age_ms = (
                (time.time() - summary.last_updated_at.timestamp()) * 1000 if summary.last_updated_at else None
            )
            if not summary.has_cache:
                task = asyncio.create_task(gs_client.update_working_status())
                _bg_tasks.add(task)
                task.add_done_callback(lambda t: _bg_tasks.discard(t))
            last_updated_at = summary.last_updated_at or datetime.now()
            commit_info_data, ahead_behind, branch_name, worktree_last_error = _compute_status(worktree_path)
            # Prefer gitstatusd-reported last_error if present
            if summary.last_error:
                worktree_last_error = summary.last_error
            wt_info = index.get_by_path(worktree_path)
            if wt_info:
                wtid_cached = wt_info.wtid
            else:
                # Fallback: find matching PR service by path when index not yet updated
                svc_wtid = None
                for svc in prs.values():
                    if svc.worktree_info.path == worktree_path:
                        svc_wtid = svc.worktree_info.wtid
                        break
                wtid_cached = svc_wtid or make_worktree_id(worktree_path.name)
            pr_info = prs.get_pr_info_cached(wtid_cached)
            # In WT_TEST_MODE, synchronously refresh once if PR cache not ready yet
            if isinstance(pr_info, PRInfoDisabled) and is_test_mode():
                await prs.refresh_now(wtid_cached)
                pr_info = prs.get_pr_info_cached(wtid_cached)
            prs.schedule_pr_refresh(wtid_cached, branch_name)
            is_cached = summary.has_cache
            is_stale = bool(cache_age_ms and timedelta(milliseconds=cache_age_ms) > config.cache_refresh_age)
            state = GitstatusdState.RUNNING if gs_client.is_running else GitstatusdState.STOPPED
        except TimeoutError:
            single_time = (time.perf_counter() - single_start) * 1000
            state = GitstatusdState.STARTING
            dirty_count, untracked_count = 0, 0
            commit_info_data, ahead_behind, branch_name, _ = _compute_status(worktree_path)
            last_updated_at = datetime.now()
            pr_info = PRInfoDisabled()
            is_cached = False
            cache_age_ms = None
            is_stale = False
            worktree_last_error = "gitstatusd timeout"

        commit_info = CommitInfo.model_validate(commit_info_data) if commit_info_data else None
        wtid = make_worktree_id(worktree_path.name)
        single_time = (time.perf_counter() - single_start) * 1000
        return (
            wtid,
            StatusResult(
                wtid=wtid,
                name=worktree_path.name,
                absolute_path=worktree_path,
                branch_name=branch_name,
                dirty_files_lower_bound=dirty_count,
                untracked_files_lower_bound=untracked_count,
                processing_time_ms=single_time,
                last_updated_at=last_updated_at,
                is_cached=is_cached,
                cache_age_ms=cache_age_ms,
                is_stale=is_stale,
                commit_info=commit_info,
                ahead_count=ahead_behind[0],
                behind_count=ahead_behind[1],
                is_main=worktree_path.resolve() == config.main_repo.resolve(),
                upstream_branch=config.upstream_branch,
                pr_info=pr_info,
                gitstatusd_state=state,
                restarts=0,
                last_error=worktree_last_error,
            ),
            single_time,
        )

    worktree_results = await asyncio.gather(*[process_single_worktree(p) for p in worktree_paths])
    total_time = 0.0
    for wtid, status_result, proc_ms in worktree_results:
        items[wtid] = StatusItem(status=status_result, processing_time_ms=proc_ms)
        total_time += proc_ms
    total_wt = len(worktree_paths)
    with_git = sum(1 for p in (gitstat.get_client(pth) for pth in worktree_paths) if p and p.is_running)
    any_wt_error = any(item.status.last_error for item in items.values())
    github_state = ComponentState.DISABLED
    if config.github_enabled:
        services = prs.values()
        if services:
            github_state = ComponentState.OK
            for prsvc in services:
                if prsvc.cached is None:
                    github_state = ComponentState.STARTING
                    break
        else:
            github_state = ComponentState.ERROR
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
        items=dict(items.items()),
        total_processing_time_ms=total_time,
        concurrent_requests=len(worktree_paths),
        daemon_health=health.health(),
        readiness_summary=readiness,
        components=components,
    )
