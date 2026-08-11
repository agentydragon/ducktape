"""The index's load-bearing invariants: only the tip is searchable, and the cache outlives it."""

from __future__ import annotations

import datetime
import math
from collections.abc import Sequence
from pathlib import Path

import pygit2
import pytest
import pytest_bazel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from haku.state_index.chunking import CHUNKER_VERSION
from haku.state_index.git_tree import list_tip
from haku.state_index.schema import Chunk
from haku.state_index.store import current_state, read_indexed_text, search
from haku.state_index.sync import sync

_AUTHOR = pygit2.Signature("Test", "test@example.com")
_NOW = datetime.datetime(2026, 8, 11, tzinfo=datetime.UTC)

# A vector space with one axis per marker word, so "which document is about beta" has an
# answer a test can assert. The floor keeps every vector non-zero: cosine distance against a
# zero vector is undefined, and a chunk mentioning none of the markers is normal.
_MARKERS = ("alpha", "beta", "gamma", "delta", "epsilon", "zeta", "eta", "theta")


class FakeEmbedder:
    """Deterministic marker-word embedder — no model weights in a database test."""

    model_key = "fake-v1"
    dims = len(_MARKERS)

    def _vector(self, text: str) -> list[float]:
        counts = [text.lower().count(marker) + 0.01 for marker in _MARKERS]
        norm = math.sqrt(sum(count * count for count in counts))
        return [count / norm for count in counts]

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vector(text)


class ExplodingEmbedder(FakeEmbedder):
    """Fails once it has embedded anything at all, to cut a sync off mid-flight."""

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        raise RuntimeError("embedder unavailable")


@pytest.fixture
def repo(tmp_path: Path) -> pygit2.Repository:
    return pygit2.init_repository(str(tmp_path / "repo.git"), bare=True, initial_head="main")


def commit(repo: pygit2.Repository, files: dict[str, str]) -> str:
    index = pygit2.Index()
    for path, content in files.items():
        index.add(pygit2.IndexEntry(path, repo.create_blob(content.encode()), pygit2.enums.FileMode.BLOB))
    parents = [] if repo.head_is_unborn else [str(repo.references["refs/heads/main"].target)]
    return str(repo.create_commit("refs/heads/main", _AUTHOR, _AUTHOR, "c", index.write_tree(repo), parents))


async def run_sync(session: AsyncSession, repo: pygit2.Repository, commit_sha: str, embedder: FakeEmbedder):
    report = await sync(session, repo, commit_sha, branch="main", embedder=embedder, now=_NOW)
    await session.commit()
    return report


async def find(session: AsyncSession, embedder: FakeEmbedder, query: str, **kwargs):
    return await search(
        session,
        embedder.embed_query(query),
        chunker_version=CHUNKER_VERSION,
        model_key=embedder.model_key,
        limit=5,
        **kwargs,
    )


async def test_search_returns_the_matching_path(session: AsyncSession, repo: pygit2.Repository) -> None:
    embedder = FakeEmbedder()
    head = commit(repo, {"notes/a.md": "all about alpha", "notes/b.md": "beta beta beta", "c.md": "gamma"})
    await run_sync(session, repo, head, embedder)

    hits = await find(session, embedder, "beta")

    assert hits[0].path == "notes/b.md"
    assert hits[0].score > 0


async def test_path_prefix_narrows_the_search(session: AsyncSession, repo: pygit2.Repository) -> None:
    embedder = FakeEmbedder()
    head = commit(repo, {"notes/a.md": "beta here", "other/b.md": "beta beta beta"})
    await run_sync(session, repo, head, embedder)

    hits = await find(session, embedder, "beta", path_prefix="notes/")

    assert [hit.path for hit in hits] == ["notes/a.md"]


async def test_deleted_content_becomes_unreachable(session: AsyncSession, repo: pygit2.Repository) -> None:
    embedder = FakeEmbedder()
    await run_sync(session, repo, commit(repo, {"keep.md": "alpha", "gone.md": "zeta zeta"}), embedder)
    await run_sync(session, repo, commit(repo, {"keep.md": "alpha"}), embedder)

    assert [hit.path for hit in await find(session, embedder, "zeta")] == ["keep.md"]
    assert await read_indexed_text(session, "gone.md", chunker_version=CHUNKER_VERSION, model_key="fake-v1") is None


async def test_deleted_content_keeps_its_cached_embedding(session: AsyncSession, repo: pygit2.Repository) -> None:
    """The cache is content-addressed, so leaving the tip must not cost the vector."""
    embedder = FakeEmbedder()
    first = commit(repo, {"keep.md": "alpha", "gone.md": "zeta zeta"})
    gone_sha = next(entry.blob_sha for entry in list_tip(repo, first) if entry.path == "gone.md")
    await run_sync(session, repo, first, embedder)
    await run_sync(session, repo, commit(repo, {"keep.md": "alpha"}), embedder)

    cached = await session.execute(select(func.count()).select_from(Chunk).where(Chunk.blob_sha == gone_sha))

    assert cached.scalar_one() > 0


async def test_restoring_deleted_content_costs_no_embedding(session: AsyncSession, repo: pygit2.Repository) -> None:
    embedder = FakeEmbedder()
    await run_sync(session, repo, commit(repo, {"keep.md": "alpha", "gone.md": "zeta zeta"}), embedder)
    await run_sync(session, repo, commit(repo, {"keep.md": "alpha"}), embedder)
    report = await run_sync(session, repo, commit(repo, {"keep.md": "alpha", "back.md": "zeta zeta"}), embedder)

    assert report.blobs_embedded == 0
    assert next(hit.path for hit in await find(session, embedder, "zeta")) == "back.md"


async def test_resync_of_an_unchanged_tip_embeds_nothing(session: AsyncSession, repo: pygit2.Repository) -> None:
    embedder = FakeEmbedder()
    head = commit(repo, {"a.md": "alpha", "b.md": "beta"})
    first = await run_sync(session, repo, head, embedder)
    second = await run_sync(session, repo, head, embedder)

    assert first.blobs_embedded == 2
    assert (second.blobs_embedded, second.blobs_reused) == (0, 2)


async def test_a_failed_sync_leaves_the_previous_tip_searchable(session: AsyncSession, repo: pygit2.Repository) -> None:
    embedder = FakeEmbedder()
    first = commit(repo, {"a.md": "alpha"})
    await run_sync(session, repo, first, embedder)

    with pytest.raises(RuntimeError):
        await sync(
            session,
            repo,
            commit(repo, {"a.md": "alpha", "b.md": "beta"}),
            branch="main",
            embedder=ExplodingEmbedder(),
            now=_NOW,
        )
    await session.rollback()

    state = await current_state(session)
    assert state is not None
    assert state.commit_sha == first
    assert [hit.path for hit in await find(session, embedder, "alpha")] == ["a.md"]


async def test_binary_and_oversized_blobs_stay_out_of_the_index(session: AsyncSession, repo: pygit2.Repository) -> None:
    embedder = FakeEmbedder()
    index = pygit2.Index()
    index.add(pygit2.IndexEntry("a.md", repo.create_blob(b"alpha"), pygit2.enums.FileMode.BLOB))
    index.add(pygit2.IndexEntry("logo.png", repo.create_blob(b"\x89PNG\x00\xff\xfe"), pygit2.enums.FileMode.BLOB))
    head = str(repo.create_commit("refs/heads/main", _AUTHOR, _AUTHOR, "c", index.write_tree(repo), []))

    report = await run_sync(session, repo, head, embedder)

    assert (report.tip_files, report.skipped_binary) == (2, 1)
    assert all(hit.path != "logo.png" for hit in await find(session, embedder, "alpha"))


if __name__ == "__main__":
    pytest_bazel.main()
