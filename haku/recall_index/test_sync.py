"""The index's load-bearing invariants: only the tip is searchable, and the cache outlives it."""

from __future__ import annotations

import datetime
from pathlib import Path

import pygit2
import pytest
import pytest_bazel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from haku.recall_index.content import content_sha
from haku.recall_index.embedder import EMBED_BATCH
from haku.recall_index.embedding_sync import embed_pending
from haku.recall_index.fake_embedder import ExplodingEmbedder, FakeEmbedder
from haku.recall_index.schema import Content, ContentEmbedding, IndexType
from haku.recall_index.store import (
    ContentRow,
    current_git_state,
    insert_contents,
    read_indexed_text,
    register_index,
    search_git,
)
from haku.recall_index.sync import AlreadyCurrent, SyncOutcome, SyncReport, sync

_AUTHOR = pygit2.Signature("Test", "test@example.com")
_NOW = datetime.datetime(2026, 8, 11, tzinfo=datetime.UTC)
_GIT_INDEX = "test-git"


@pytest.fixture
def repo(tmp_path: Path) -> pygit2.Repository:
    return pygit2.init_repository(str(tmp_path / "repo.git"), bare=True, initial_head="main")


def commit(repo: pygit2.Repository, files: dict[str, str]) -> str:
    index = pygit2.Index()
    for path, content in files.items():
        index.add(pygit2.IndexEntry(path, repo.create_blob(content.encode()), pygit2.enums.FileMode.BLOB))
    parents = [] if repo.head_is_unborn else [str(repo.references["refs/heads/main"].target)]
    return str(repo.create_commit("refs/heads/main", _AUTHOR, _AUTHOR, "c", index.write_tree(repo), parents))


async def embed_all(session: AsyncSession, embedder: FakeEmbedder) -> int:
    total = 0
    while (report := await embed_pending(session, embedder=embedder)).contents_embedded:
        total += report.contents_embedded
        await session.commit()
    return total


async def run_sync(
    session: AsyncSession,
    repo: pygit2.Repository,
    commit_sha: str,
    embedder: FakeEmbedder,
    *,
    index_id: str = _GIT_INDEX,
) -> SyncOutcome:
    await register_index(session, index_id, index_type=IndexType.GIT)
    outcome = await sync(session, repo, commit_sha, index_id=index_id, branch="main", now=_NOW)
    await session.commit()
    await embed_all(session, embedder)
    return outcome


def as_report(outcome: SyncOutcome) -> SyncReport:
    """Narrow to the did-work variant, failing the test if the sync took the early-out."""
    assert isinstance(outcome, SyncReport)
    return outcome


async def find(
    session: AsyncSession,
    embedder: FakeEmbedder,
    query: str,
    *,
    index_id: str = _GIT_INDEX,
    path_prefix: str | None = None,
):
    return await search_git(
        session,
        await embedder.embed_query(query),
        index_id=index_id,
        model_key=embedder.model_key,
        limit=5,
        path_prefix=path_prefix,
    )


async def test_search_returns_the_matching_path(
    session: AsyncSession, repo: pygit2.Repository, embedder: FakeEmbedder
) -> None:
    head = commit(repo, {"notes/a.md": "all about alpha", "notes/b.md": "beta beta beta", "c.md": "gamma"})
    await run_sync(session, repo, head, embedder)

    hits = await find(session, embedder, "beta")

    assert hits[0].path == "notes/b.md"
    assert hits[0].score > 0


async def test_path_prefix_narrows_the_search(
    session: AsyncSession, repo: pygit2.Repository, embedder: FakeEmbedder
) -> None:
    head = commit(repo, {"notes/a.md": "beta here", "other/b.md": "beta beta beta"})
    await run_sync(session, repo, head, embedder)

    hits = await find(session, embedder, "beta", path_prefix="notes/")

    assert [hit.path for hit in hits] == ["notes/a.md"]


async def test_two_git_indexes_share_vectors_but_not_occurrences(
    session: AsyncSession, tmp_path: Path, embedder: FakeEmbedder
) -> None:
    """The future read boundary is real in storage before an API is allowed to expose it."""
    first = pygit2.init_repository(str(tmp_path / "first.git"), bare=True, initial_head="main")
    second = pygit2.init_repository(str(tmp_path / "second.git"), bare=True, initial_head="main")
    await register_index(session, "first", index_type=IndexType.GIT)
    await register_index(session, "second", index_type=IndexType.GIT)
    await session.commit()

    await run_sync(session, first, commit(first, {"a.md": "shared alpha"}), embedder, index_id="first")
    await run_sync(session, second, commit(second, {"b.md": "shared beta"}), embedder, index_id="second")

    assert [hit.path for hit in await find(session, embedder, "shared", index_id="first")] == ["a.md"]
    assert [hit.path for hit in await find(session, embedder, "shared", index_id="second")] == ["b.md"]
    first_state = await current_git_state(session, "first")
    second_state = await current_git_state(session, "second")
    assert first_state is not None
    assert first_state.commit_sha is not None
    assert second_state is not None
    assert second_state.commit_sha is not None


async def test_deleted_content_becomes_unreachable(
    session: AsyncSession, repo: pygit2.Repository, embedder: FakeEmbedder
) -> None:
    await run_sync(session, repo, commit(repo, {"keep.md": "alpha", "gone.md": "zeta zeta"}), embedder)
    await run_sync(session, repo, commit(repo, {"keep.md": "alpha"}), embedder)

    assert [hit.path for hit in await find(session, embedder, "zeta")] == ["keep.md"]
    assert await read_indexed_text(session, "gone.md", index_id=_GIT_INDEX, model_key="fake-v1") is None


async def test_deleted_content_keeps_its_cached_embedding(
    session: AsyncSession, repo: pygit2.Repository, embedder: FakeEmbedder
) -> None:
    """The cache is content-addressed, so leaving the tip must not cost the vector."""
    first = commit(repo, {"keep.md": "alpha", "gone.md": "zeta zeta"})
    await run_sync(session, repo, first, embedder)
    await run_sync(session, repo, commit(repo, {"keep.md": "alpha"}), embedder)

    cached = await session.execute(
        select(func.count())
        .select_from(ContentEmbedding)
        .where(ContentEmbedding.content_sha == content_sha("zeta zeta"))
    )

    assert cached.scalar_one() > 0


async def test_restoring_deleted_content_costs_no_embedding(
    session: AsyncSession, repo: pygit2.Repository, embedder: FakeEmbedder
) -> None:
    await run_sync(session, repo, commit(repo, {"keep.md": "alpha", "gone.md": "zeta zeta"}), embedder)
    await run_sync(session, repo, commit(repo, {"keep.md": "alpha"}), embedder)
    report = as_report(
        await run_sync(session, repo, commit(repo, {"keep.md": "alpha", "back.md": "zeta zeta"}), embedder)
    )

    # Both blobs reuse their existing chunk rows; restoring a known blob writes no source content.
    assert report.contents_materialized == 0
    assert next(hit.path for hit in await find(session, embedder, "zeta")) == "back.md"


async def test_resync_of_an_unchanged_tip_does_no_work(
    session: AsyncSession, repo: pygit2.Repository, embedder: FakeEmbedder
) -> None:
    """The early-out is what lets a push trigger and a reconciling cron both fire freely."""
    head = commit(repo, {"a.md": "alpha", "b.md": "beta"})
    first = as_report(await run_sync(session, repo, head, embedder))
    second = await run_sync(session, repo, head, embedder)

    assert first.contents_materialized == 2
    assert second == AlreadyCurrent(commit_sha=head)
    assert next(hit.path for hit in await find(session, embedder, "alpha")) == "a.md"


async def test_a_changed_model_re_embeds_without_re_syncing_the_source(
    session: AsyncSession, repo: pygit2.Repository
) -> None:
    """A new vector space drains the shared content queue without re-reading Git."""
    head = commit(repo, {"a.md": "alpha", "b.md": "beta"})
    await run_sync(session, repo, head, FakeEmbedder())

    successor = FakeEmbedder(model_key="fake-v2")
    assert await sync(session, repo, head, index_id=_GIT_INDEX, branch="main", now=_NOW) == AlreadyCurrent(
        commit_sha=head
    )
    assert await embed_all(session, successor) == 2
    assert next(hit.path for hit in await find(session, successor, "beta")) == "b.md"


async def test_binary_and_oversized_blobs_stay_out_of_the_index(
    session: AsyncSession, repo: pygit2.Repository, embedder: FakeEmbedder
) -> None:
    index = pygit2.Index()
    index.add(pygit2.IndexEntry("a.md", repo.create_blob(b"alpha"), pygit2.enums.FileMode.BLOB))
    index.add(pygit2.IndexEntry("logo.png", repo.create_blob(b"\x89PNG\x00\xff\xfe"), pygit2.enums.FileMode.BLOB))
    index.add(pygit2.IndexEntry("drawing.svg", repo.create_blob(b"<svg>\x00</svg>"), pygit2.enums.FileMode.BLOB))
    head = str(repo.create_commit("refs/heads/main", _AUTHOR, _AUTHOR, "c", index.write_tree(repo), []))

    report = as_report(await run_sync(session, repo, head, embedder))

    assert (report.tip_files, report.skipped_binary) == (3, 2)
    assert {hit.path for hit in await find(session, embedder, "alpha")} == {"a.md"}


async def test_a_failed_embedding_worker_leaves_the_source_tip_published(
    session: AsyncSession, repo: pygit2.Repository, embedder: FakeEmbedder
) -> None:
    """Source progress does not wait for a provider; a later worker retry fills vectors."""
    head = commit(repo, {"a.md": "alpha", "b.md": "beta"})
    await register_index(session, _GIT_INDEX, index_type=IndexType.GIT)
    report = as_report(await sync(session, repo, head, index_id=_GIT_INDEX, branch="main", now=_NOW))
    await session.commit()

    assert report.contents_materialized == 2
    state = await current_git_state(session, _GIT_INDEX)
    assert state is not None
    assert state.commit_sha == head
    assert await find(session, embedder, "alpha") == []
    with pytest.raises(RuntimeError):
        await embed_pending(session, embedder=ExplodingEmbedder())
    await session.rollback()

    assert await embed_all(session, embedder) == 2
    assert {hit.path for hit in await find(session, embedder, "alpha")} == {"a.md", "b.md"}


async def test_shared_embedding_worker_drains_source_material_in_bounded_batches(
    session: AsyncSession, repo: pygit2.Repository, embedder: FakeEmbedder
) -> None:
    """The worker, not a source sync, chooses the provider request size."""
    head = commit(repo, {f"n{number}.md": f"text {number}" for number in range(EMBED_BATCH + 1)})
    await register_index(session, _GIT_INDEX, index_type=IndexType.GIT)
    await sync(session, repo, head, index_id=_GIT_INDEX, branch="main", now=_NOW)
    await session.commit()

    first = await embed_pending(session, embedder=embedder)
    await session.commit()
    second = await embed_pending(session, embedder=embedder)

    assert (first.contents_embedded, second.contents_embedded) == (EMBED_BATCH, 1)


async def test_materializing_more_than_asyncpg_parameter_limit_batches_content_queries(session: AsyncSession) -> None:
    """A large Git tip must not make one ``IN`` query exceed asyncpg's 32,767-argument limit."""
    count = 33_000

    await insert_contents(
        session, (ContentRow(content_sha=f"{number:064x}", content=f"content {number}") for number in range(count))
    )
    await session.commit()

    result = await session.execute(select(func.count()).select_from(Content))
    assert result.scalar_one() == count


if __name__ == "__main__":
    pytest_bazel.main()
