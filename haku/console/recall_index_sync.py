"""Keep every configured recall index current, from the haku-indexer worker (``indexer.py``).

The deploy-owned recall-index registry says both *what* is indexed and how it is sourced.  A
logical index is the unit of synchronization, advisory leadership, status, and — later — read
authorization.  There are no implicit ``haku-state`` or conversations indexes in this module.

A sweep never runs on the request path — the console serves search from the last committed index
state — so a failure is logged and retried on the next tick while search continues to serve the
last published source revision.
"""

from __future__ import annotations

import asyncio
import datetime
import hashlib
import logging
import os
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager, suppress

import pygit2
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from haku.console.config import ChatRecallIndexDefinition, ConfiguredRecallIndex, GitRecallIndexDefinition
from haku.recall_index.chat_sync import ChatSyncReport, sync_chat
from haku.recall_index.chunking import DEFAULT_CHUNK_BUDGET, ChunkBudget
from haku.recall_index.embedder import Embedder
from haku.recall_index.embedding_sync import EmbeddingSyncReport, embed_pending
from haku.recall_index.git_tree import fetch_branch, open_mirror, remote_tip
from haku.recall_index.schema import IndexType
from haku.recall_index.store import current_git_state, record_remote_tip, register_index
from haku.recall_index.sync import AlreadyCurrent, SyncOutcome, is_current, sync

logger = logging.getLogger(__name__)

DEFAULT_CHAT_INTERVAL = datetime.timedelta(seconds=60)
# As short as chat's, because the common tick is one `ls-remote` — refs only, no objects — and
# fetching happens only when the tip actually moved.
DEFAULT_GIT_INTERVAL = datetime.timedelta(seconds=30)
DEFAULT_EMBED_INTERVAL = datetime.timedelta(seconds=5)


def advisory_lock_for(index_id: str) -> int:
    """The stable Postgres advisory-lock key for one configured logical index."""
    return int.from_bytes(
        hashlib.blake2b(f"recall-index:{index_id}".encode(), digest_size=8).digest(), byteorder="big", signed=True
    )


@asynccontextmanager
async def _leading(engine: AsyncEngine, scope: str) -> AsyncIterator[bool]:
    """Hold one cross-replica maintenance lock, or yield false to the current leader."""
    lock = advisory_lock_for(scope)
    async with engine.connect() as leader:
        if not await leader.scalar(text("SELECT pg_try_advisory_lock(:lock)"), {"lock": lock}):
            yield False
            return
        try:
            yield True
        finally:
            if not await leader.scalar(text("SELECT pg_advisory_unlock(:lock)"), {"lock": lock}):
                logger.error("Recall maintenance %s advisory lock %#x was not held at release", scope, lock)


def _git_credentials(index: GitRecallIndexDefinition) -> tuple[str | None, str | None]:
    if index.username_env_var is None:
        return None, None
    username = os.environ.get(index.username_env_var)
    password = os.environ.get(index.password_env_var or "")
    if username is None or password is None:
        raise RuntimeError(f"Git recall index {index.index_id!r} is missing configured credentials")
    return username, password


def _open_and_peek(index: GitRecallIndexDefinition) -> tuple[pygit2.Repository, str | None]:
    """Open one configured mirror and inspect its remote branch. Blocking; called in a thread."""
    username, password = _git_credentials(index)
    repository = open_mirror(index.mirror_path, index.repo_url, username=username, password=password)
    return repository, remote_tip(repository, index.branch, username=username, password=password)


def _fetch(repository: pygit2.Repository, index: GitRecallIndexDefinition) -> str:
    """Bring one configured mirror up to its remote branch. Blocking; called in a thread."""
    username, password = _git_credentials(index)
    return fetch_branch(repository, index.branch, username=username, password=password)


class RecallIndexMaintenance:
    """Materialize configured source chunks, with one cross-replica leader per index."""

    def __init__(
        self,
        engine: AsyncEngine,
        sessions: async_sessionmaker[AsyncSession],
        *,
        indexes: Iterable[ConfiguredRecallIndex],
        budget: ChunkBudget = DEFAULT_CHUNK_BUDGET,
    ) -> None:
        self._engine = engine
        self._sessions = sessions
        self._indexes = tuple(indexes)
        self._budget = budget

    async def sync_index_once(self, index: ConfiguredRecallIndex) -> SyncOutcome | ChatSyncReport | None:
        """Synchronize one explicitly configured index, if this replica wins its lock."""
        async with _leading(self._engine, f"source:{index.index_id}") as leading:
            if not leading:
                return None
            async with self._sessions() as session:
                await register_index(session, index.index_id, index_type=IndexType(index.index_type))
                if isinstance(index, ChatRecallIndexDefinition):
                    report = await sync_chat(
                        session, index_id=index.index_id, now=datetime.datetime.now(datetime.UTC), budget=self._budget
                    )
                    await session.commit()
                    if report.sessions_indexed or report.sessions_forgotten:
                        logger.info(
                            "Chat index %s: %d sessions indexed, %d forgotten, %d windows written (%d content values materialized)",
                            index.index_id,
                            report.sessions_indexed,
                            report.sessions_forgotten,
                            report.windows_written,
                            report.contents_materialized,
                        )
                    return report

                repository, tip = await asyncio.to_thread(_open_and_peek, index)
                if tip is None:
                    logger.error("Git index %s remote has no branch %r", index.index_id, index.branch)
                    return None
                await record_remote_tip(
                    session, tip, index_id=index.index_id, branch=index.branch, now=datetime.datetime.now(datetime.UTC)
                )
                await session.commit()
                if is_current(
                    await current_git_state(session, index.index_id), tip, branch=index.branch, budget=self._budget
                ):
                    return AlreadyCurrent(commit_sha=tip)
                commit_sha = await asyncio.to_thread(_fetch, repository, index)
                outcome = await sync(
                    session,
                    repository,
                    commit_sha,
                    index_id=index.index_id,
                    branch=index.branch,
                    now=datetime.datetime.now(datetime.UTC),
                    budget=self._budget,
                )
                await session.commit()
        if not isinstance(outcome, AlreadyCurrent):
            logger.info(
                "Git index %s: %s, %d files, %d chunks written (%d content values materialized)",
                index.index_id,
                outcome.commit_sha[:12],
                outcome.tip_files,
                outcome.chunks_written,
                outcome.contents_materialized,
            )
        return outcome

    async def sync_all_once(self) -> None:
        for index in self._indexes:
            await self.sync_index_once(index)

    async def _sweep(self, index: ConfiguredRecallIndex, interval: datetime.timedelta) -> None:
        while True:
            try:
                await self.sync_index_once(index)
            except Exception:
                logger.exception("Recall index %s sync sweep failed", index.index_id)
            await asyncio.sleep(interval.total_seconds())

    @asynccontextmanager
    async def run(
        self,
        *,
        chat_interval: datetime.timedelta = DEFAULT_CHAT_INTERVAL,
        git_interval: datetime.timedelta = DEFAULT_GIT_INTERVAL,
    ) -> AsyncIterator[None]:
        """Sweep every configured index until application shutdown."""
        sweeps = [
            asyncio.create_task(
                self._sweep(index, chat_interval if isinstance(index, ChatRecallIndexDefinition) else git_interval),
                name=f"recall-index-sync-{index.index_id}",
            )
            for index in self._indexes
        ]
        try:
            yield
        finally:
            for sweep in sweeps:
                sweep.cancel()
            for sweep in sweeps:
                with suppress(asyncio.CancelledError):
                    await sweep


class RecallEmbeddingMaintenance:
    """Drain one model's globally shared content queue off the source-sync path."""

    def __init__(self, engine: AsyncEngine, sessions: async_sessionmaker[AsyncSession], *, embedder: Embedder) -> None:
        self._engine = engine
        self._sessions = sessions
        self._embedder = embedder

    async def embed_once(self) -> EmbeddingSyncReport | None:
        async with _leading(self._engine, f"embedding:{self._embedder.model_key}") as leading:
            if not leading:
                return None
            async with self._sessions() as session:
                report = await embed_pending(session, embedder=self._embedder)
                await session.commit()
        if report.contents_embedded:
            logger.info(
                "Recall embedding model %s: %d content values embedded",
                self._embedder.model_key,
                report.contents_embedded,
            )
        return report

    async def _sweep(self, interval: datetime.timedelta) -> None:
        while True:
            try:
                report = await self.embed_once()
            except Exception:
                logger.exception("Recall embedding sweep for model %s failed", self._embedder.model_key)
                report = None
            if report is None or report.contents_embedded == 0:
                await asyncio.sleep(interval.total_seconds())

    @asynccontextmanager
    async def run(self, *, interval: datetime.timedelta = DEFAULT_EMBED_INTERVAL) -> AsyncIterator[None]:
        sweep = asyncio.create_task(self._sweep(interval), name=f"recall-index-embed-{self._embedder.model_key}")
        try:
            yield
        finally:
            sweep.cancel()
            with suppress(asyncio.CancelledError):
                await sweep
