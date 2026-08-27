"""The embedding drain's claiming discipline: concurrent claimers never double-embed."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

import pytest_bazel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from haku.recall_index.content import content_sha
from haku.recall_index.embedding_sync import embed_pending
from haku.recall_index.fake_embedder import FakeEmbedder
from haku.recall_index.schema import ContentEmbedding
from haku.recall_index.store import ContentRow, insert_contents


class RecordingEmbedder(FakeEmbedder):
    """Counts every document sent to the provider — the cost a duplicate claim would incur."""

    def __init__(self) -> None:
        super().__init__()
        self.embedded: list[str] = []

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        self.embedded.extend(texts)
        return await super().embed_documents(texts)


async def test_concurrent_claimers_drain_disjoint_batches(engine: AsyncEngine) -> None:
    """Two drain transactions overlapping in time split the queue instead of sharing it.

    The second claim runs while the first transaction still holds its rows, exactly the state two
    embed replicas are in during a slow provider call. Conflict-safe insertion alone would keep
    the *rows* consistent while both replicas paid for the same embeddings — hence the recorder:
    the invariant is that no content is sent to the provider twice.
    """
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    contents = [f"queued document {n}" for n in range(6)]
    async with sessions() as seeding:
        await insert_contents(seeding, [ContentRow(content_sha=content_sha(text), content=text) for text in contents])
        await seeding.commit()

    embedder = RecordingEmbedder()
    async with sessions() as first, sessions() as second:
        first_report = await embed_pending(first, embedder=embedder, limit=3)
        # `first` has not committed: its batch is still claimed when the second drain selects.
        # The bound is how a wrong claiming discipline fails fast rather than wedging the test:
        # a claimless select re-reads the first batch, and its conflicting vector insert then
        # blocks on `first`'s open transaction instead of returning.
        async with asyncio.timeout(30):
            second_report = await embed_pending(second, embedder=embedder, limit=3)
        await first.commit()
        await second.commit()

    assert first_report.contents_embedded == 3
    assert second_report.contents_embedded == 3
    assert sorted(embedder.embedded) == sorted(contents)

    async with sessions() as check:
        assert await check.scalar(select(func.count()).select_from(ContentEmbedding)) == len(contents)


if __name__ == "__main__":
    pytest_bazel.main()
