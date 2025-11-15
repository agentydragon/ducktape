from __future__ import annotations

from pathlib import Path

from ...shared.configuration import Configuration
from ...shared.constants import MAIN_WORKTREE_DISPLAY_NAME
from ...shared.protocol import (
    ErrorCodes,
    TeleportCdThere,
    TeleportDoesNotExist,
    WorktreeResolvePathParams,
    WorktreeResolvePathResult,
    WorktreeTeleportTargetParams,
)
from ..rpc import RpcError, rpc
from ..services import WorktreeIndexService


def _find_target_worktree(index: WorktreeIndexService, worktree_name: str | None, current_path: Path):
    if worktree_name == MAIN_WORKTREE_DISPLAY_NAME and index.main():
        return index.main(), None
    resolved = index.resolve_target(worktree_name, current_path)
    if not resolved:
        raise RpcError(
            code=ErrorCodes.WORKTREE_NOT_FOUND, message=f"Worktree '{worktree_name or current_path}' not found"
        )
    return resolved


def _resolve_path_spec(
    path_spec: str, target_path: Path, current_relative_path: str | None, is_current_worktree: bool
) -> Path:
    if path_spec.startswith("/"):
        return target_path / path_spec.lstrip("/")
    if path_spec.startswith("./"):
        if not is_current_worktree:
            raise RpcError(code=ErrorCodes.INVALID_PARAMS, message="Cannot use relative path for different worktree")
        current_dir = target_path / current_relative_path if current_relative_path else target_path
        return (current_dir / path_spec).resolve()
    return target_path / path_spec


def _compute_teleport_target_path(target_path: Path, relative_path: str | None) -> Path:
    """Compute the absolute target Path for teleport, returning a Path.

    This keeps internal code working with Path objects; RPC boundary converts to str.
    """
    if not relative_path or relative_path == ".":
        return target_path
    candidate = target_path / relative_path
    if candidate.exists() and candidate.is_dir():
        return candidate
    return target_path


@rpc.method("worktree_resolve_path", params=WorktreeResolvePathParams)
async def handle_resolve_path(
    index: WorktreeIndexService, config: Configuration, params: WorktreeResolvePathParams
) -> WorktreeResolvePathResult:
    current_path = params.current_path
    target_worktree, current_relative_path = _find_target_worktree(index, params.worktree_name, current_path)
    resolved_path = _resolve_path_spec(
        params.path_spec, target_worktree.path, current_relative_path, params.worktree_name is None
    )
    return WorktreeResolvePathResult(absolute_path=resolved_path)


@rpc.method("worktree_teleport_target", params=WorktreeTeleportTargetParams)
async def handle_teleport_target(
    index: WorktreeIndexService, config: Configuration, params: WorktreeTeleportTargetParams
) -> TeleportCdThere | TeleportDoesNotExist:
    current_path = params.current_path
    target_wt = (
        index.main() if params.target_name == MAIN_WORKTREE_DISPLAY_NAME else index.get_by_name(params.target_name)
    )
    if not target_wt:
        return TeleportDoesNotExist(type="does_not_exist", name=params.target_name)
    resolved = index.resolve_target(None, current_path)
    relative_path = resolved[1] if resolved else None
    cd_path = _compute_teleport_target_path(target_wt.path, relative_path)
    return TeleportCdThere(type="cd_there", cd_path=cd_path)
