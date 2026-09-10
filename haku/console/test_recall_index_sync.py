"""Configured-index maintenance and reader integration tests."""

from __future__ import annotations

import datetime
from pathlib import Path

import pygit2
import pytest
import pytest_bazel
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from haku.console.mcp_config import ConsoleConfigFile
from haku.console.recall_index_reader import PostgresIndexSearcher
from haku.console.recall_index_sync import RecallEmbeddingMaintenance, RecallIndexMaintenance, advisory_lock_for
from haku.console.tools.recall_index import GitIndexStatus
from haku.recall_index.config import GitRecallIndexDefinition
from haku.recall_index.fake_embedder import ExplodingEmbedder, FakeEmbedder
from haku.recall_index.schema import ContentEmbedding

_AUTHOR = pygit2.Signature("Test", "test@example.com")
_NOW = datetime.datetime(2026, 8, 15, tzinfo=datetime.UTC)
_MANUAL_AUTHORITY_CONFIG = {
    "auto_approval_policies": [{"id": "manual", "type": "never"}],
    "access_profiles": [{"id": "manual", "auto_approval_policy": "manual"}],
    "default_access_profile_id": "manual",
}


def test_recall_profile_grants_require_declared_indexes() -> None:
    config = ConsoleConfigFile.model_validate(
        {
            **_MANUAL_AUTHORITY_CONFIG,
            "recall_indexes": {
                "ducktape_public": {"index_id": "ducktape-public", "index_type": "git", "repo_url": "https://example"}
            },
            "access_profiles": [
                {"id": "manual", "auto_approval_policy": "manual", "recall_index_ids": ["ducktape-public"]}
            ],
        }
    )
    assert config.access_profiles[0].recall_index_ids == {"ducktape-public"}

    with pytest.raises(ValueError, match="unknown Recall indexes"):
        ConsoleConfigFile.model_validate(
            {
                **_MANUAL_AUTHORITY_CONFIG,
                "recall_indexes": {
                    "ducktape_public": {
                        "index_id": "ducktape-public",
                        "index_type": "git",
                        "repo_url": "https://example",
                    }
                },
                "access_profiles": [
                    {"id": "manual", "auto_approval_policy": "manual", "recall_index_ids": ["haku-state"]}
                ],
            }
        )


def test_profile_in_process_server_grants_require_configured_in_process_servers() -> None:
    configured = {
        **_MANUAL_AUTHORITY_CONFIG,
        "mcp": {
            "servers": {
                "haku_index": {"id": "haku_index", "backend": {"kind": "in_process", "credential": {"kind": "none"}}}
            }
        },
        "access_profiles": [
            {"id": "manual", "auto_approval_policy": "manual", "in_process_server_ids": ["haku_index"]}
        ],
    }
    config = ConsoleConfigFile.model_validate(configured)
    assert config.access_profiles[0].in_process_server_ids == {"haku_index"}

    with pytest.raises(ValueError, match="unknown in-process MCP servers"):
        ConsoleConfigFile.model_validate(
            {
                **configured,
                "access_profiles": [
                    {"id": "manual", "auto_approval_policy": "manual", "in_process_server_ids": ["missing"]}
                ],
            }
        )


@pytest.fixture
def embedder() -> FakeEmbedder:
    return FakeEmbedder()


def _git_index(tmp_path: Path, *, index_id: str, content: bytes) -> GitRecallIndexDefinition:
    """A configured Git index backed by a bare repository with one main-branch commit."""
    origin = pygit2.init_repository(str(tmp_path / f"{index_id}-origin.git"), bare=True, initial_head="main")
    index = pygit2.Index()
    blob = origin.create_blob(content)
    index.add(pygit2.IndexEntry("notes/alpha.md", blob, pygit2.enums.FileMode.BLOB))
    origin.create_commit("refs/heads/main", _AUTHOR, _AUTHOR, "seed", index.write_tree(origin), [])
    return GitRecallIndexDefinition(
        index_id=index_id,
        repo_url=str(tmp_path / f"{index_id}-origin.git"),
        mirror_path=tmp_path / f"{index_id}-mirror.git",
    )


@pytest.fixture
def haku_state(tmp_path: Path) -> GitRecallIndexDefinition:
    return _git_index(tmp_path, index_id="haku-state", content=b"user: the egress fence keys on haku-sandbox\n")


def maintenance(
    engine: AsyncEngine, sessions: async_sessionmaker[AsyncSession], *indexes: GitRecallIndexDefinition
) -> RecallIndexMaintenance:
    return RecallIndexMaintenance(engine, sessions, indexes=indexes)


async def synchronize_and_embed(
    engine: AsyncEngine,
    sessions: async_sessionmaker[AsyncSession],
    embedder: FakeEmbedder,
    *indexes: GitRecallIndexDefinition,
) -> None:
    await maintenance(engine, sessions, *indexes).sync_all_once()
    worker = RecallEmbeddingMaintenance(sessions, embedder=embedder)
    while (await worker.embed_once()).contents_embedded:
        pass


async def test_every_configured_index_is_synchronized_and_individually_searchable(
    migrated_engine: AsyncEngine,
    migrated_sessions: async_sessionmaker[AsyncSession],
    haku_state: GitRecallIndexDefinition,
    tmp_path: Path,
    embedder: FakeEmbedder,
) -> None:
    ducktape_public = _git_index(tmp_path, index_id="ducktape-public", content=b"a public egress note\n")
    indexes = (haku_state, ducktape_public)
    await synchronize_and_embed(migrated_engine, migrated_sessions, embedder, *indexes)

    searcher = PostgresIndexSearcher(migrated_sessions, embedder, indexes=indexes)
    haku_state_results = await searcher.search("egress", index_id="haku-state", limit=5)
    public_results = await searcher.search("egress", index_id="ducktape-public", limit=5)
    assert {hit.source.kind for hit in haku_state_results.hits} == {"git"}
    assert {hit.source.index_id for hit in haku_state_results.hits} == {"haku-state"}
    assert {hit.source.kind for hit in public_results.hits} == {"git"}
    assert {hit.source.index_id for hit in public_results.hits} == {"ducktape-public"}


async def test_identical_content_across_configured_indexes_shares_one_embedding(
    migrated_engine: AsyncEngine,
    migrated_sessions: async_sessionmaker[AsyncSession],
    haku_state: GitRecallIndexDefinition,
    tmp_path: Path,
    embedder: FakeEmbedder,
) -> None:
    same_content = b"user: the egress fence keys on haku-sandbox\n"
    other = _git_index(tmp_path, index_id="ducktape-public", content=same_content)
    await synchronize_and_embed(migrated_engine, migrated_sessions, embedder, haku_state, other)
    async with migrated_sessions() as session:
        assert await session.scalar(select(func.count()).select_from(ContentEmbedding)) == 1


async def test_status_reads_all_configured_indexes_not_fixed_names(
    migrated_engine: AsyncEngine,
    migrated_sessions: async_sessionmaker[AsyncSession],
    haku_state: GitRecallIndexDefinition,
    tmp_path: Path,
    embedder: FakeEmbedder,
) -> None:
    other = _git_index(tmp_path, index_id="ducktape-public", content=b"status source\n")
    indexes = (haku_state, other)
    await synchronize_and_embed(migrated_engine, migrated_sessions, embedder, *indexes)
    status = await PostgresIndexSearcher(migrated_sessions, embedder, indexes=indexes).status(
        index_ids=("haku-state", "ducktape-public")
    )
    assert [(entry.index_id, entry.index_type) for entry in status.indexes] == [
        ("haku-state", "git"),
        ("ducktape-public", "git"),
    ]


async def test_source_current_but_embedding_pending_reports_the_remote_tip_and_pending_work(
    migrated_engine: AsyncEngine,
    migrated_sessions: async_sessionmaker[AsyncSession],
    haku_state: GitRecallIndexDefinition,
    embedder: FakeEmbedder,
) -> None:
    indexes = (haku_state,)
    await maintenance(migrated_engine, migrated_sessions, *indexes).sync_index_once(haku_state)
    worker = RecallEmbeddingMaintenance(migrated_sessions, embedder=ExplodingEmbedder())
    with pytest.raises(RuntimeError):
        await worker.embed_once()

    searcher = PostgresIndexSearcher(migrated_sessions, embedder, indexes=indexes)
    status = await searcher.status(index_ids=("haku-state",))
    (git,) = status.indexes
    assert isinstance(git, GitIndexStatus)
    assert git.indexed_commit == git.remote_commit
    assert git.branch == "main"
    assert git.pending_chunks == 1
    results = await searcher.search("egress", index_id=haku_state.index_id, limit=5)
    assert results.hits == []
    assert results.index is not None


async def test_a_replica_that_loses_one_index_lock_leaves_that_index_alone(
    migrated_engine: AsyncEngine,
    migrated_sessions: async_sessionmaker[AsyncSession],
    haku_state: GitRecallIndexDefinition,
) -> None:
    async with migrated_engine.connect() as leader:
        lock = advisory_lock_for(f"source:{haku_state.index_id}")
        assert await leader.scalar(text("SELECT pg_try_advisory_lock(:lock)"), {"lock": lock})
        assert await maintenance(migrated_engine, migrated_sessions, haku_state).sync_index_once(haku_state) is None


def test_git_index_credentials_are_explicit_and_paired() -> None:
    with pytest.raises(ValueError, match="password"):
        GitRecallIndexDefinition(
            index_id="private-notes",
            repo_url="https://example.invalid/private-notes.git",
            credentials={"username": "private-notes"},
        )


if __name__ == "__main__":
    pytest_bazel.main()
