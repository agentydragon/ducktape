from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
import logging
from typing import cast

import pygit2

from wt.shared.fixtures import load_pr_fixture

from ..shared.configuration import Configuration
from ..shared.env import is_test_mode
from ..shared.error_handling import GitHubUnavailableError
from ..shared.github_models import PRData, PRState
from .git_manager import GitManager
from .github_client import GitHubInterface
from .github_refresh import DebouncedGitHubRefresh
from .types import DiscoveredWorktree

PR_CACHE_FRESH_SECS = 60

logger = logging.getLogger(__name__)


# Runtime-only algebraic cache (not serialized)
@dataclass
class PRCacheOk:
    data: PRData
    fetched_at: datetime


@dataclass
class PRCacheError:
    error: str
    fetched_at: datetime


@dataclass
class PRCacheDisabled:
    fetched_at: datetime


class PRService:
    """Manages GitHub PR cache for a single worktree (server-side)."""

    def __init__(
        self,
        github_interface: GitHubInterface | None,
        config: Configuration,
        worktree_info: DiscoveredWorktree,
        git_manager: GitManager,
    ) -> None:
        self.github_interface = github_interface
        self.config = config
        self.worktree_info = worktree_info
        self.git_manager = git_manager
        self.cached = None
        self.github_refresh = None

    github_interface: GitHubInterface | None
    config: Configuration
    worktree_info: DiscoveredWorktree
    git_manager: GitManager
    cached: PRCacheOk | PRCacheError | PRCacheDisabled | None = None
    github_refresh: DebouncedGitHubRefresh | None = None

    async def start(self) -> None:
        if self.github_interface:
            self.github_refresh = DebouncedGitHubRefresh(
                self.worktree_info.path,
                self._refresh_github_cache,
                debounce_delay=self.config.github_debounce_delay.total_seconds(),
                periodic_interval=self.config.github_periodic_interval.total_seconds(),
            )
            await self.github_refresh.start()
            # Populate PR cache synchronously so first status already has PR info
            await self._refresh_github_cache("startup", [])

    async def stop(self) -> None:
        if self.github_refresh:
            await self.github_refresh.stop()

    async def _refresh_github_cache(self, reason: str, files_changed: list[str]):
        logger.debug(
            "PRService: refresh requested (%s) for %s; files_changed=%d",
            reason,
            self.worktree_info.path,
            len(files_changed),
        )
        try:
            repo_obj = self.git_manager.get_repo(self.worktree_info.path)
            branch_name = repo_obj.head.shorthand or ""
        except (pygit2.GitError, OSError, ValueError) as e:
            logger.warning(
                "PRService: skipping refresh for missing/invalid worktree %s: %s", self.worktree_info.path, e
            )
            self.cached = PRCacheError(error=str(e), fetched_at=datetime.now())
            return
        await self.get_pr_info(branch_name, force_refresh=True)

    async def get_pr_info(self, branch_name: str, force_refresh: bool = False) -> PRData | None:
        now = datetime.now()
        if (
            not force_refresh
            and self.cached is not None
            and (now - self.cached.fetched_at).total_seconds() < PR_CACHE_FRESH_SECS
        ):
            if isinstance(self.cached, PRCacheOk):
                return self.cached.data
            # When cache holds a non-OK state, treat as missing PR
            return None
        # Test mode: optional PR fixture support, avoids PYTHONPATH/mock imports in tests
        if is_test_mode():
            fixture_pr = load_pr_fixture(self.config, branch_name)
            if fixture_pr is not None:
                self.cached = PRCacheOk(data=fixture_pr, fetched_at=now)
                return cast(PRData, fixture_pr)
            # In test mode, absence of a fixture means "no PR" — do not hit real GitHub
            self.cached = PRCacheDisabled(fetched_at=now)
            return None

        if not self.github_interface:
            self.cached = PRCacheDisabled(fetched_at=now)
            return None
        pr_info: PRData | None = None
        try:
            # github_interface is guaranteed non-None by check above
            gh = self.github_interface

            loop = asyncio.get_running_loop()
            prs = await loop.run_in_executor(None, gh.pr_search, branch_name)
            if prs:
                pr = prs[0]
                pr_info = PRData(
                    pr_number=int(pr.number),
                    pr_state=PRState(pr.state),
                    draft=bool(pr.draft),
                    mergeable=pr.mergeable,
                    merged_at=(pr.merged_at.isoformat() if pr.merged_at else None),
                    additions=pr.additions,
                    deletions=pr.deletions,
                )
        except (OSError, RuntimeError) as e:
            logger.warning("PR fetch failed for %s: %s", branch_name, e)
            self.cached = PRCacheError(error=str(e), fetched_at=now)
            return None
        except GitHubUnavailableError as e:
            logger.warning("PR fetch GitHub unavailable for %s: %s", branch_name, e)
            self.cached = PRCacheError(error=str(e), fetched_at=now)
            return None
        else:
            if pr_info is not None:
                self.cached = PRCacheOk(data=pr_info, fetched_at=now)
            else:
                self.cached = PRCacheDisabled(fetched_at=now)
            return pr_info
