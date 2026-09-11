"""Flux tar artifacts through durable publication and the authenticated search API."""

import asyncio
import hashlib
import io
import tarfile
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import cast
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_bazel
from pydantic import SecretStr

from haku.recall_index.fake_embedder import FakeEmbedder
from util.kubernetes import CustomObjectsClient
from x.agentplane.indexing.app import create_app
from x.agentplane.indexing.maintenance import Maintenance
from x.agentplane.indexing.source import ArchiveLimits, FluxSource
from x.agentplane.indexing.store import Store


@dataclass
class Harness:
    custom_objects: AsyncMock
    maintenance: Maintenance
    client: httpx.AsyncClient
    digests: list[str]


@pytest.fixture
async def harness(store: Store, embedder: FakeEmbedder) -> AsyncIterator[Harness]:
    archives: list[bytes] = []
    for text in (b"alpha old document\n", b"beta new document\n"):
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w:gz") as archive:
            member = tarfile.TarInfo("document.txt")
            member.size = len(text)
            archive.addfile(member, io.BytesIO(text))
        archives.append(output.getvalue())
    digests = ["sha256:" + hashlib.sha256(archive).hexdigest() for archive in archives]
    custom_objects = AsyncMock(spec=CustomObjectsClient)
    custom_objects.get_namespaced_custom_object.return_value = {
        "metadata": {"generation": 1},
        "spec": {"url": "https://git.test/index-fixture.git"},
        "status": {
            "observedGeneration": 1,
            "conditions": [{"type": "Ready", "status": "True"}],
            "artifact": {"url": "http://source.test/0", "digest": digests[0], "revision": "test@sha1:old"},
        },
    }

    def download(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=httpx.ByteStream(archives[int(request.url.path.removeprefix("/"))]))

    async with httpx.AsyncClient(transport=httpx.MockTransport(download)) as artifact_http:
        source = FluxSource(
            custom_objects=cast(CustomObjectsClient, custom_objects),
            http=artifact_http,
            namespace="test-flux",
            name="test-index-fixture",
            limits=ArchiveLimits(),
        )
        maintenance = Maintenance(
            store=store, source=source, embedder=embedder, poll_seconds=60, gc_seconds=60, gc_grace=timedelta(days=1)
        )
        app = create_app(
            store=store, embedder=embedder, maintenance=maintenance, read_token=SecretStr("test-read-token")
        )
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://index.test") as client:
            yield Harness(custom_objects=custom_objects, maintenance=maintenance, client=client, digests=digests)


async def test_read_api_authentication(harness: Harness) -> None:
    assert (await harness.client.get("/healthz")).status_code == 200
    for headers in ({}, {"Authorization": "Bearer wrong-token"}):
        assert (await harness.client.get("/status", headers=headers)).status_code == 401
        assert (await harness.client.post("/search", json={"query": "alpha"}, headers=headers)).status_code == 401
    duplicate = [("Authorization", "Bearer test-read-token"), ("Authorization", "Bearer test-read-token")]
    assert (await harness.client.get("/status", headers=duplicate)).status_code == 401
    assert (await harness.client.get("/status", headers={"Authorization": "Bearer test-read-token"})).status_code == 200


async def test_flux_update_serves_old_revision_when_embedding_fails(
    harness: Harness, store: Store, embedder: FakeEmbedder, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers = {"Authorization": "Bearer test-read-token"}
    await harness.maintenance.sync_once()
    assert await store.advance(embedder)
    initial = await harness.client.post("/search", json={"query": "alpha"}, headers=headers)
    assert initial.status_code == 200
    assert initial.json()["warning"] is None
    assert initial.json()["hits"][0]["revision"] == "test@sha1:old"

    harness.custom_objects.get_namespaced_custom_object.return_value["status"]["artifact"] = {
        "url": "http://source.test/1",
        "digest": harness.digests[1],
        "revision": "test@sha1:new",
    }
    await harness.maintenance.sync_once()
    updating = await harness.client.post("/search", json={"query": "alpha"}, headers=headers)
    assert updating.json()["warning"] is not None
    assert updating.json()["status"]["index"]["pending_files"] == 1
    assert updating.json()["hits"][0]["revision"] == "test@sha1:old"

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
        assert body["status"]["index"]["desired_revision"] == "test@sha1:new"
        assert body["status"]["index"]["completed_revision"] == "test@sha1:old"
        assert body["hits"][0]["text"] == "alpha old document\n"
        assert body["hits"][0]["revision"] == "test@sha1:old"
        assert body["hits"][0]["artifact_digest"] == harness.digests[0]
        assert body["hits"][0]["repository_url"] == "https://git.test/index-fixture.git"

    monkeypatch.undo()
    assert await store.advance(embedder)
    recovered = await harness.client.post("/search", json={"query": "beta"}, headers=headers)
    assert recovered.json()["hits"][0]["text"] == "beta new document\n"
    assert recovered.json()["hits"][0]["revision"] == "test@sha1:new"
    assert recovered.json()["status"]["index"]["pending_files"] == 0


if __name__ == "__main__":
    pytest_bazel.main()
