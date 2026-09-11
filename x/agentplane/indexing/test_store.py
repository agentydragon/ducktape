"""Publication, cache reuse, restart and retention guarantees."""

import asyncio
from collections.abc import Sequence
from datetime import timedelta

import pytest
import pytest_bazel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from haku.recall_index.chunking import DEFAULT_CHUNK_BUDGET, ChunkBudget
from haku.recall_index.content import content_sha
from haku.recall_index.fake_embedder import ExplodingEmbedder, FakeEmbedder
from x.agentplane.indexing.store import Blob, Content, Embedding, Snapshot, Store

REPOSITORY = "https://example.test/repo.git"


async def ingest(store: Store, revision: str, files: dict[str, bytes]) -> None:
    await store.ingest(digest=revision, revision=revision, repository_url=REPOSITORY, files=files)


async def drain(store: Store, embedder: FakeEmbedder) -> None:
    while await store.advance(embedder):
        pass


async def test_per_file_publication_and_restart(store: Store, embedder: FakeEmbedder, engine: AsyncEngine) -> None:
    await ingest(store, "A", {"a": b"alpha", "b": b"beta", "deleted": b"gamma"})
    await drain(store, embedder)
    await ingest(store, "B", {"a": b"alpha new", "b": b"beta new"})
    before, _ = await store.search(await embedder.embed_query("alpha"))
    assert {hit.path for hit in before} == {"a", "b"}
    assert {hit.revision for hit in before} == {"A"}
    assert (await store.status()).updating
    await store.advance(embedder)
    mixed, _ = await store.search(await embedder.embed_query("alpha"))
    assert {hit.path: hit.revision for hit in mixed} == {"a": "B", "b": "A"}
    restarted = Store(engine, budget=DEFAULT_CHUNK_BUDGET, model_key=embedder.model_key)
    await restarted.initialize()
    await drain(restarted, embedder)
    status = await restarted.status()
    assert status.completed_revision == "B"
    assert not status.updating


async def test_failed_embedding_preserves_old_file(store: Store, embedder: FakeEmbedder) -> None:
    await ingest(store, "A", {"a": b"alpha"})
    await drain(store, embedder)
    await ingest(store, "B", {"a": b"beta"})
    with pytest.raises(RuntimeError, match="unavailable"):
        await store.advance(ExplodingEmbedder())
    assert (await store.search(await embedder.embed_query("alpha")))[0][0].revision == "A"
    assert (await store.status()).pending_files == 1
    await ingest(store, "C", {"a": b"gamma"})
    await drain(store, embedder)
    assert (await store.search(await embedder.embed_query("gamma")))[0][0].revision == "C"


async def test_identical_content_reuses_embeddings_and_updates_citations(store: Store, embedder: FakeEmbedder) -> None:
    await ingest(store, "A", {"a": b"alpha", "b": b"alpha"})
    await drain(store, embedder)
    async with store.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Embedding)) == 1
    await store.ingest(digest="A", revision="B", repository_url=REPOSITORY, files={"a": b"alpha", "b": b"alpha"})
    assert not await store.advance(ExplodingEmbedder())
    assert {hit.revision for hit in (await store.search(await embedder.embed_query("alpha")))[0]} == {"B"}


async def test_invalid_utf8_replacement_removes_old_hits(store: Store, embedder: FakeEmbedder) -> None:
    await ingest(store, "A", {"changed.txt": b"alpha", "unicode.data": "café".encode()})
    await drain(store, embedder)
    await ingest(store, "B", {"changed.txt": b"alpha\xff", "unicode.data": "café".encode()})
    # Excluding the replacement needs no provider call, even though it has a valid prefix.
    assert await store.advance(ExplodingEmbedder())
    hits, status = await store.search(await embedder.embed_query("alpha"))
    assert [(hit.path, hit.text) for hit in hits] == [("unicode.data", "café")]
    assert status.completed_revision == "B"
    assert not status.updating


async def test_empty_binary_and_empty_snapshot_complete(store: Store, embedder: FakeEmbedder) -> None:
    await ingest(store, "A", {"empty": b"", "binary": b"\xff", "nul": b"a\x00b"})
    await drain(store, embedder)
    assert not (await store.status()).updating
    assert (await store.search(await embedder.embed_query("alpha")))[0] == []
    await ingest(store, "B", {})
    assert (await store.status()).served_files == 0
    assert (await store.status()).completed_revision == "B"


async def test_gc_retains_served_versions_then_collects_orphans(store: Store, embedder: FakeEmbedder) -> None:
    await ingest(store, "A", {"a": b"alpha"})
    await drain(store, embedder)
    await ingest(store, "B", {"a": b"beta"})
    for _ in range(5):
        await store.gc(grace=timedelta(0))
    assert (await store.search(await embedder.embed_query("alpha")))[0][0].revision == "A"
    await drain(store, embedder)
    assert await store.gc(grace=timedelta(days=1)) == 0
    for _ in range(5):
        await store.gc(grace=timedelta(0), batch_size=1)
    async with store.sessions() as session:
        for entity in (Snapshot, Blob, Content, Embedding):
            assert await session.scalar(select(func.count()).select_from(entity)) == 1


async def test_batches_are_durable_before_publication(engine: AsyncEngine, embedder: FakeEmbedder) -> None:
    store = Store(engine, budget=ChunkBudget(target_bytes=5, max_bytes=5), model_key=embedder.model_key)
    await store.initialize()
    await ingest(store, "A", {"a": b"alpha beta gamma"})
    assert await store.advance(embedder, batch_size=1)
    assert (await store.status()).served_files == 0
    async with store.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Embedding)) == 1
    await drain(store, embedder)
    assert (await store.status()).served_files == 1


class GatedEmbedder(FakeEmbedder):
    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        self.entered.set()
        await self.release.wait()
        return await super().embed_documents(texts)


async def test_search_remains_available_during_embedding(store: Store, embedder: FakeEmbedder) -> None:
    await ingest(store, "A", {"a": b"alpha"})
    await drain(store, embedder)
    await ingest(store, "B", {"a": b"beta"})
    gated = GatedEmbedder()
    task = asyncio.create_task(store.advance(gated))
    try:
        async with asyncio.timeout(10):
            await gated.entered.wait()
            hits, status = await store.search(await embedder.embed_query("alpha"))
            assert hits[0].revision == "A"
            assert status.updating
    finally:
        gated.release.set()
        await task


async def test_configuration_change_rejected(store: Store, engine: AsyncEngine) -> None:
    changed = Store(engine, budget=DEFAULT_CHUNK_BUDGET, model_key="another-model")
    with pytest.raises(ValueError, match="configuration changed"):
        await changed.initialize()


async def test_gc_revival_restarts_grace(store: Store, embedder: FakeEmbedder) -> None:
    await ingest(store, "A", {"a": b"alpha"})
    await drain(store, embedder)
    await ingest(store, "B", {})
    await store.gc(grace=timedelta(days=1))
    await ingest(store, "A", {"a": b"alpha"})
    await drain(store, embedder)
    await ingest(store, "B", {})
    # A became reachable and unreachable again between sweeps. Its old mark cannot
    # authorize deletion on this first observation of the new unreachable period.
    assert await store.gc(grace=timedelta(0)) == 0


class WrongDimensionEmbedder(FakeEmbedder):
    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return [[1.0, 0.0] for _ in texts]


async def test_backend_dimension_change_preserves_served_version(store: Store, embedder: FakeEmbedder) -> None:
    await ingest(store, "A", {"a": b"alpha"})
    await drain(store, embedder)
    await ingest(store, "B", {"a": b"beta"})
    with pytest.raises(ValueError, match="dimensions changed"):
        await store.advance(WrongDimensionEmbedder())
    hits, status = await store.search(await embedder.embed_query("alpha"))
    assert hits[0].revision == "A"
    assert status.updating


async def test_new_layout_revives_cached_content(engine: AsyncEngine, embedder: FakeEmbedder) -> None:
    store = Store(engine, budget=ChunkBudget(target_bytes=5, max_bytes=5), model_key=embedder.model_key)
    await store.initialize()
    await ingest(store, "A", {"a": b"alpha"})
    await drain(store, embedder)
    await ingest(store, "B", {})
    for _ in range(3):
        await store.gc(grace=timedelta(0))
    async with store.sessions() as session:
        content = await session.get(Content, content_sha("alpha"))
        assert content is not None
        assert content.unreachable_since is not None
    await ingest(store, "C", {"a": b"alphabeta"})
    await drain(store, embedder)
    async with store.sessions() as session:
        content = await session.get(Content, content_sha("alpha"))
        assert content is not None
        assert content.unreachable_since is None


if __name__ == "__main__":
    pytest_bazel.main()
