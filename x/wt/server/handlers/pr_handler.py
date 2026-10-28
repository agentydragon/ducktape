from __future__ import annotations

from x.wt.server.rpc import ServiceDependencies, rpc
from x.wt.shared.protocol import PRRefreshParams


@rpc.method("pr_refresh_now", params=PRRefreshParams)
async def pr_refresh_now(deps: ServiceDependencies, params: PRRefreshParams) -> str:
    """Refresh PR cache synchronously.

    The centralized GitHubWatcher fetches all branches in a batch,
    so we refresh the entire cache rather than per-worktree.
    """
    if deps.github_watcher is None:
        raise RuntimeError("GitHub watcher is not configured")
    await deps.github_watcher.refresh_now()
    return "ok"
