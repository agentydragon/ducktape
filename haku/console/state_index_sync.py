"""Keeping the haku index current, from inside the console.

Both corpora are synced by the process that already holds them: `chat` is built from the
console's own `claude_chat_messages`, and `git` from a read-only mirror of haku-state that this
process fetches. One deployment, no CronJob, and `index_status` stops reporting a backlog that
nothing drains.

**Each corpus gets its own advisory lock and its own task.** Only one replica syncs a corpus at
a time, and a git fetch that takes a minute must not hold up a chat sweep that takes a second —
they are unrelated work that happens to share a database.

A sweep never runs on the request path, so a failure here is logged and retried on the next tick
rather than surfaced: searches keep serving what is already indexed, and `index_status` is what
says how stale that is.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress

import pygit2
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from haku.console.config import HakuStateGitConfig
from haku.state_index.chat_sync import sync_chat
from haku.state_index.embedder import Embedder
from haku.state_index.git_tree import fetch_branch, open_mirror
from haku.state_index.sync import AlreadyCurrent, sync

logger = logging.getLogger(__name__)

DEFAULT_CHAT_INTERVAL = datetime.timedelta(seconds=60)
# Longer than chat's: it costs a network fetch, and the repository is written by hand rather than
# by every turn of every conversation.
DEFAULT_GIT_INTERVAL = datetime.timedelta(minutes=5)

# Public because leader election is a contract a test (and an operator with psql) checks, not an
# implementation detail of this class.
CHAT_ADVISORY_LOCK = 0x48414B55494E4443
GIT_ADVISORY_LOCK = 0x48414B55494E4447


def _fetch_tip(git: HakuStateGitConfig) -> tuple[pygit2.Repository, str]:
    """Clone-or-fetch the mirror and return it at the branch tip. Blocking; called in a thread."""
    password = None if git.password is None else git.password.get_secret_value()
    repository = open_mirror(git.mirror_path, git.repo_url, username=git.username, password=password)
    return repository, fetch_branch(repository, git.branch, username=git.username, password=password)


class StateIndexMaintenance:
    """Sync sweeps for the index's two corpora, one leader replica at a time."""

    def __init__(
        self,
        engine: AsyncEngine,
        sessions: async_sessionmaker[AsyncSession],
        *,
        embedder: Embedder,
        git: HakuStateGitConfig | None,
    ) -> None:
        self._engine = engine
        self._sessions = sessions
        self._embedder = embedder
        self._git = git

    @asynccontextmanager
    async def _leading(self, lock: int) -> AsyncIterator[bool]:
        """Hold `lock` for the body, or run the body with False if another replica holds it."""
        async with self._engine.connect() as leader:
            if not await leader.scalar(text("SELECT pg_try_advisory_lock(:lock)"), {"lock": lock}):
                yield False
                return
            try:
                yield True
            finally:
                if not await leader.scalar(text("SELECT pg_advisory_unlock(:lock)"), {"lock": lock}):
                    logger.error("Index sync advisory lock %#x was not held at release", lock)

    async def sync_chat_once(self) -> None:
        async with self._leading(CHAT_ADVISORY_LOCK) as leading:
            if not leading:
                return
            async with self._sessions() as session:
                report = await sync_chat(session, embedder=self._embedder, now=datetime.datetime.now(datetime.UTC))
                await session.commit()
        if report.sessions_indexed or report.sessions_forgotten:
            logger.info(
                "Chat index: %d sessions indexed, %d forgotten, %d windows written (%d embedded, %d reused)",
                report.sessions_indexed,
                report.sessions_forgotten,
                report.windows_written,
                report.windows_embedded,
                report.windows_reused,
            )

    async def sync_git_once(self) -> None:
        if (git := self._git) is None:
            return
        async with self._leading(GIT_ADVISORY_LOCK) as leading:
            if not leading:
                return
            # libgit2's clone and fetch are blocking, and the first one on a fresh pod clones the
            # whole repository — off the event loop, which is also serving the operator's console.
            repository, commit_sha = await asyncio.to_thread(_fetch_tip, git)
            async with self._sessions() as session:
                outcome = await sync(
                    session,
                    repository,
                    commit_sha,
                    branch=git.branch,
                    embedder=self._embedder,
                    now=datetime.datetime.now(datetime.UTC),
                )
                await session.commit()
        if isinstance(outcome, AlreadyCurrent):
            return
        logger.info(
            "haku-state index: %s, %d files, %d chunks written (%d blobs embedded, %d reused)",
            outcome.commit_sha[:12],
            outcome.tip_files,
            outcome.chunks_written,
            outcome.blobs_embedded,
            outcome.blobs_reused,
        )

    async def _sweep(self, corpus: str, once: Callable[[], Awaitable[None]], interval: datetime.timedelta) -> None:
        while True:
            try:
                await once()
            except Exception:
                logger.exception("%s index sync sweep failed", corpus)
            await asyncio.sleep(interval.total_seconds())

    @asynccontextmanager
    async def run(
        self,
        *,
        chat_interval: datetime.timedelta = DEFAULT_CHAT_INTERVAL,
        git_interval: datetime.timedelta = DEFAULT_GIT_INTERVAL,
    ) -> AsyncIterator[None]:
        """Sweep both corpora until application shutdown."""
        sweeps = [asyncio.create_task(self._sweep("chat", self.sync_chat_once, chat_interval), name="index-sync-chat")]
        if self._git is not None:
            sweeps.append(
                asyncio.create_task(self._sweep("haku-state", self.sync_git_once, git_interval), name="index-sync-git")
            )
        try:
            yield
        finally:
            for sweep in sweeps:
                sweep.cancel()
            for sweep in sweeps:
                with suppress(asyncio.CancelledError):
                    await sweep
