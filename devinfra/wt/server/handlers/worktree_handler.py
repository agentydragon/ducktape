from __future__ import annotations

import logging
from pathlib import Path

from wt.server.git_manager import GitManager
from wt.server.rpc import RpcError, ServiceDependencies, Stream, rpc
from wt.server.services import WorktreeCoordinator, WorktreeIndexService
from wt.server.types import DiscoveredWorktree
from wt.server.worktree_ids import make_worktree_id, parse_worktree_id, wtid_to_path
from wt.server.worktree_service import WorktreeService
from wt.shared.configuration import Configuration
from wt.shared.constants import MAIN_WORKTREE_DISPLAY_NAME
from wt.shared.protocol import (
    ErrorCodes,
    HookOutputEvent,
    HookRunResult,
    HookStream,
    ProgressEvent,
    ProgressOperation,
    WorktreeCreateParams,
    WorktreeCreateResult,
    WorktreeCreateStep,
    WorktreeDeleteParams,
    WorktreeDeleteResult,
    WorktreeGetByNameParams,
    WorktreeGetByNameResult,
    WorktreeIdentifyParams,
    WorktreeIdentifyResult,
    WorktreeInfo,
    WorktreeListResult,
)

logger = logging.getLogger(__name__)


@rpc.method("worktree_list")
async def worktree_list(git_manager: GitManager, config: Configuration) -> WorktreeListResult:
    worktrees: list[WorktreeInfo] = []
    for info in git_manager.list_worktrees():
        if info.is_main:
            continue
        worktree_name = info.path.name
        worktree_id = make_worktree_id(worktree_name)
        worktrees.append(
            WorktreeInfo(
                wtid=worktree_id,
                name=worktree_name,
                absolute_path=info.path,
                branch_name=info.branch,
                exists=info.exists,
                is_main=False,
            )
        )

    return WorktreeListResult(worktrees=worktrees)


@rpc.stream("worktree_create", params=WorktreeCreateParams)
async def worktree_create(
    deps: ServiceDependencies, svc: WorktreeService, params: WorktreeCreateParams, stream: Stream
) -> WorktreeCreateResult:
    git_manager = deps.git_manager
    coordinator = deps.coordinator
    config = deps.config
    if "/" in params.name:
        raise RpcError(code=ErrorCodes.INVALID_PARAMS, message=f"Worktree name '{params.name}' cannot contain slashes")
    worktree_path = config.worktrees_dir / params.name
    branch_name = f"{config.branch_prefix}{params.name}"
    worktree_id = make_worktree_id(params.name)
    if worktree_path.exists():
        raise RpcError(code=ErrorCodes.INVALID_PARAMS, message=f"Worktree path {worktree_path} already exists")
    source_path = None
    if params.source_wtid:
        source_path = wtid_to_path(config, params.source_wtid)
        if not source_path.exists():
            raise RpcError(code=ErrorCodes.WORKTREE_NOT_FOUND, message=f"Source worktree {source_path} not found")
        src_branch = git_manager.get_repo_head_shorthand(source_path)
    elif params.source_branch:
        src_branch = params.source_branch
    else:
        src_branch = config.upstream_branch

    # Emit progress events around the slow hydration/checkout step
    def _emit_progress(step: WorktreeCreateStep, message: str, progress: float):
        stream.emit(
            ProgressEvent(operation=ProgressOperation.WORKTREE_CREATE, step=step, progress=progress, message=message)
        )

    if source_path:
        _emit_progress(WorktreeCreateStep.HYDRATE_STARTED, "hydrate started", 0.0)
    else:
        _emit_progress(WorktreeCreateStep.CHECKOUT_STARTED, "checkout started", 0.0)

    svc.create_worktree(config, params.name, source_worktree=source_path, source_branch=src_branch)
    # Update daemon registry and index immediately (index-only lookup pathway)
    wt_info = DiscoveredWorktree(worktree_path, worktree_path.name, make_worktree_id(params.name))
    await coordinator.register_worktree(wt_info)

    if source_path:
        _emit_progress(WorktreeCreateStep.HYDRATE_DONE, "hydrate done", 1.0)
    else:
        _emit_progress(WorktreeCreateStep.CHECKOUT_DONE, "checkout done", 1.0)

    post = None
    if config.post_creation_script:
        # run_post_creation_script is async; we stream via the same writer
        async def _sink(name: str, data: str) -> None:
            ev = HookOutputEvent(stream=(HookStream.STDOUT if name == "stdout" else HookStream.STDERR), output=data)
            stream.emit(ev)

        post = await WorktreeService.run_post_creation_script(
            str(config.post_creation_script),
            worktree_path,
            _sink,
            deadline=config.post_creation_timeout.total_seconds(),
        )

    return WorktreeCreateResult(
        wtid=worktree_id,
        name=params.name,
        absolute_path=worktree_path,
        branch_name=branch_name,
        success=True,
        post_hook=(HookRunResult(**post) if post else None),
    )


@rpc.method("worktree_delete", params=WorktreeDeleteParams)
async def worktree_delete(
    coordinator: WorktreeCoordinator, svc: WorktreeService, config: Configuration, params: WorktreeDeleteParams
) -> WorktreeDeleteResult:
    worktree_name = parse_worktree_id(params.wtid)
    worktree_path = config.worktrees_dir / worktree_name
    if not worktree_path.exists():
        raise RpcError(
            code=ErrorCodes.WORKTREE_NOT_FOUND, message=f"Worktree {worktree_name} does not exist at {worktree_path}"
        )
    await svc.remove_worktree(config, worktree_name, force=params.force)

    wt_info = DiscoveredWorktree(worktree_path, worktree_name, make_worktree_id(worktree_name))
    await coordinator.unregister_worktree(wt_info)
    return WorktreeDeleteResult(wtid=params.wtid, success=True, message=f"Deleted worktree {worktree_name}")


def _resolve_worktree_name_to_info(index: WorktreeIndexService, name: str) -> DiscoveredWorktree | None:
    if name == MAIN_WORKTREE_DISPLAY_NAME and index.main():
        return index.main()
    return index.get_by_name(name)


@rpc.method("worktree_identify", params=WorktreeIdentifyParams)
async def worktree_identify(
    index: WorktreeIndexService, config: Configuration, params: WorktreeIdentifyParams
) -> WorktreeIdentifyResult:
    absolute_path = params.absolute_path
    # Fail fast on non-existent path
    if not absolute_path.exists():
        raise RpcError(code=ErrorCodes.WORKTREE_NOT_FOUND, message=f"{absolute_path} is not a managed worktree")

    # Determine worktree name and relative path with guard clauses (early bailout)
    if absolute_path.is_relative_to(config.worktrees_dir):
        rel_path = absolute_path.relative_to(config.worktrees_dir)
        worktree_name = rel_path.parts[0] if rel_path.parts else None
        relative_path = str(Path(*rel_path.parts[1:])) if len(rel_path.parts) > 1 else ""
    elif absolute_path.is_relative_to(config.main_repo):
        worktree_name = MAIN_WORKTREE_DISPLAY_NAME
        relative_path = str(absolute_path.relative_to(config.main_repo))
    else:
        raise RpcError(code=ErrorCodes.WORKTREE_NOT_FOUND, message=f"{absolute_path} is not a managed worktree")

    if not worktree_name:
        raise RpcError(code=ErrorCodes.WORKTREE_NOT_FOUND, message=f"{absolute_path} is not a managed worktree")

    found_worktree = _resolve_worktree_name_to_info(index, worktree_name)
    if not found_worktree:
        raise RpcError(code=ErrorCodes.WORKTREE_NOT_FOUND, message=f"{absolute_path} is not a managed worktree")

    resolved_name = (
        MAIN_WORKTREE_DISPLAY_NAME
        if found_worktree.path.resolve() == config.main_repo.resolve()
        else found_worktree.path.name
    )
    return WorktreeIdentifyResult(
        wtid=make_worktree_id(resolved_name), name=resolved_name, is_worktree=True, relative_path=relative_path
    )


@rpc.method("worktree_get_by_name", params=WorktreeGetByNameParams)
async def worktree_get_by_name(
    index: WorktreeIndexService, config: Configuration, params: WorktreeGetByNameParams
) -> WorktreeGetByNameResult:
    found_worktree = _resolve_worktree_name_to_info(index, params.name)
    if found_worktree:
        worktree_name = (
            MAIN_WORKTREE_DISPLAY_NAME
            if found_worktree.path.resolve() == config.main_repo.resolve()
            else found_worktree.path.name
        )
        result = WorktreeGetByNameResult(
            wtid=make_worktree_id(worktree_name), name=worktree_name, exists=True, absolute_path=found_worktree.path
        )
    else:
        result = WorktreeGetByNameResult(wtid=None, name=None, exists=False, absolute_path=None)
    return result
