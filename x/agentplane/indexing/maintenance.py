"""Independent source, embedding, and collection loops for one index."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta

from haku.recall_index.embedder import Embedder
from x.agentplane.indexing.source import FluxSource
from x.agentplane.indexing.store import Store

logger = logging.getLogger(__name__)


@dataclass
class Maintenance:
    store: Store
    source: FluxSource
    embedder: Embedder
    poll_seconds: float
    gc_seconds: float
    gc_grace: timedelta
    embedding_timeout_seconds: float = 60
    source_error: str | None = None
    embedding_error: str | None = None
    gc_error: str | None = None

    async def sync_once(self) -> None:
        state = await self.store.status()
        snapshot = await self.source.snapshot(
            current_digest=state.desired_digest,
            current_revision=state.desired_revision,
            current_repository_url=state.desired_repository_url,
        )
        if snapshot is not None:
            await self.store.ingest(
                digest=snapshot.digest,
                revision=snapshot.revision,
                repository_url=snapshot.repository_url,
                files=snapshot.files,
            )

    async def _sources(self) -> None:
        while True:
            try:
                await self.sync_once()
                self.source_error = None
            except Exception as exc:
                logger.exception("Flux snapshot reconciliation failed")
                self.source_error = type(exc).__name__
            await asyncio.sleep(self.poll_seconds)

    async def _embeddings(self) -> None:
        while True:
            try:
                async with asyncio.timeout(self.embedding_timeout_seconds):
                    progressed = await self.store.advance(self.embedder)
                self.embedding_error = None
            except Exception as exc:
                logger.exception("Embedding/publication failed; retaining served files")
                self.embedding_error = type(exc).__name__
                progressed = False
            await asyncio.sleep(0 if progressed else 5)

    async def _collect(self) -> None:
        while True:
            try:
                await self.store.gc(grace=self.gc_grace)
                self.gc_error = None
            except Exception as exc:
                logger.exception("Index garbage collection failed")
                self.gc_error = type(exc).__name__
            await asyncio.sleep(self.gc_seconds)

    @asynccontextmanager
    async def run(self) -> AsyncIterator[None]:
        tasks = [asyncio.create_task(loop()) for loop in (self._sources, self._embeddings, self._collect)]
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
