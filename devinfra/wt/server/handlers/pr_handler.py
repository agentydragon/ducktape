from __future__ import annotations

from wt.server.github_watcher import GitHubWatcher
from wt.server.rpc import rpc
from wt.shared.protocol import PRRefreshParams


@rpc.method("pr_refresh_now", params=PRRefreshParams)
async def pr_refresh_now(github_watcher: GitHubWatcher, params: PRRefreshParams) -> str:
    """Refresh PR cache synchronously.

    The centralized GitHubWatcher fetches all branches in a batch,
    so we refresh the entire cache rather than per-worktree.
    """
    await github_watcher.refresh_now()
    return "ok"
