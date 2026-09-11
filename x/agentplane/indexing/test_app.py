"""Git commits through durable publication and the authenticated search API."""

import asyncio
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import httpx
import pathspec
import pytest
import pytest_bazel
from pydantic import SecretStr

from haku.recall_index.fake_embedder import FakeEmbedder
from x.agentplane.indexing.app import create_app
from x.agentplane.indexing.conftest import Upstream
from x.agentplane.indexing.maintenance import Maintenance
from x.agentplane.indexing.source import GitSource, SnapshotLimits
from x.agentplane.indexing.store import Store


@dataclass
class Harness:
    upstream: Upstream
    maintenance: Maintenance
    client: httpx.AsyncClient
    revisions: list[str]


@pytest.fixture
async def harness(store: Store, embedder: FakeEmbedder, upstream: Upstream, tmp_path: Path) -> AsyncIterator[Harness]:
    revisions = [upstream.commit({"document.txt": b"alpha old document\n"})]
    source = GitSource(
        url=upstream.url,
        branch="main",
        path=tmp_path / "clone",
        credentials=None,
        ignore=pathspec.GitIgnoreSpec.from_lines([]),
        limits=SnapshotLimits(),
    )
    maintenance = Maintenance(
        store=store, source=source, embedder=embedder, poll_seconds=60, gc_seconds=60, gc_grace=timedelta(days=1)
    )
    app = create_app(store=store, embedder=embedder, maintenance=maintenance, read_token=SecretStr("test-read-token"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://index.test") as client:
        yield Harness(upstream=upstream, maintenance=maintenance, client=client, revisions=revisions)


async def test_read_api_authentication(harness: Harness) -> None:
    assert (await harness.client.get("/healthz")).status_code == 200
    for headers in ({}, {"Authorization": "Bearer wrong-token"}):
        assert (await harness.client.get("/status", headers=headers)).status_code == 401
        assert (await harness.client.post("/search", json={"query": "alpha"}, headers=headers)).status_code == 401
    duplicate = [("Authorization", "Bearer test-read-token"), ("Authorization", "Bearer test-read-token")]
    assert (await harness.client.get("/status", headers=duplicate)).status_code == 401
    assert (await harness.client.get("/status", headers={"Authorization": "Bearer test-read-token"})).status_code == 200


async def test_update_serves_old_revision_when_embedding_fails(
    harness: Harness, store: Store, embedder: FakeEmbedder, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers = {"Authorization": "Bearer test-read-token"}
    await harness.maintenance.sync_once()
    assert await store.advance(embedder)
    initial = await harness.client.post("/search", json={"query": "alpha"}, headers=headers)
    assert initial.status_code == 200
    assert initial.json()["warning"] is None
    assert initial.json()["hits"][0]["revision"] == harness.revisions[0]

    harness.revisions.append(harness.upstream.commit({"document.txt": b"beta new document\n"}))
    await harness.maintenance.sync_once()
    updating = await harness.client.post("/search", json={"query": "alpha"}, headers=headers)
    assert updating.json()["warning"] is not None
    assert updating.json()["status"]["index"]["pending_files"] == 1
    assert updating.json()["hits"][0]["revision"] == harness.revisions[0]

    failure = asyncio.Event()

    async def fail_embedding(texts: Sequence[str]) -> list[list[float]]:
        failure.set()
        raise RuntimeError("test embedding backend unavailable")

    monkeypatch.setattr(embedder, "embed_documents", fail_embedding)
    async with harness.maintenance.run():
        await asyncio.wait_for(failure.wait(), timeout=10)
        response = await harness.client.post("/search", json={"query": "alpha"}, headers=headers)
        assert response.status_code == 200
        body = response.json()
        assert body["warning"] is not None
        assert body["status"]["embedding_error"] == "RuntimeError"
        assert body["status"]["index"]["desired_revision"] == harness.revisions[1]
        assert body["status"]["index"]["completed_revision"] == harness.revisions[0]
        assert body["hits"][0]["text"] == "alpha old document\n"
        assert body["hits"][0]["revision"] == harness.revisions[0]
        assert body["hits"][0]["tree_id"] == harness.upstream.tree_id(harness.revisions[0])
        assert body["hits"][0]["repository_url"] == harness.upstream.url

    monkeypatch.undo()
    assert await store.advance(embedder)
    recovered = await harness.client.post("/search", json={"query": "beta"}, headers=headers)
    assert recovered.json()["hits"][0]["text"] == "beta new document\n"
    assert recovered.json()["hits"][0]["revision"] == harness.revisions[1]
    assert recovered.json()["status"]["index"]["pending_files"] == 0


if __name__ == "__main__":
    pytest_bazel.main()
