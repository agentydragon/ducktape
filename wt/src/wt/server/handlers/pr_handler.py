from __future__ import annotations

from ...shared.protocol import PRRefreshParams
from ..rpc import rpc
from ..services import PRServiceProvider, WorktreeIndexService


@rpc.method("pr_refresh_now", params=PRRefreshParams)
async def pr_refresh_now(prs: PRServiceProvider, index: WorktreeIndexService, params: PRRefreshParams) -> str:
    # Refresh PR cache synchronously for the given worktree id
    # Use provider helper to force-refresh using current branch
    await prs.refresh_now(params.wtid)
    return "ok"
