"""The sweeps that fill the index, against the console's real schema.

What matters here is the wiring rather than the chunking: that a sweep reads the console's own
tables and a real git remote, writes chunks the search tools then find, and that a replica which
loses the advisory lock leaves the work to the one holding it.
"""

from __future__ import annotations

import datetime
import uuid
from pathlib import Path
from uuid import UUID

import pygit2
import pytest
import pytest_bazel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from haku.console import state_index_sync
from haku.console.chat_models import ChatMessageRole, ChatMessageStatus, ChatSessionStatus
from haku.console.config import HakuStateGitConfig
from haku.console.database_schema import ClaudeChatMessage, ClaudeChatSession, Operator
from haku.console.operator_identity import OperatorStatus
from haku.console.state_index_reader import PostgresIndexSearcher
from haku.console.state_index_sync import CHAT_ADVISORY_LOCK, StateIndexMaintenance
from haku.console.tools.state_index import ConversationSource, HakuStateSource, SearchCorpus
from haku.state_index.fake_embedder import ExplodingEmbedder, FakeEmbedder

_AUTHOR = pygit2.Signature("Test", "test@example.com")
_NOW = datetime.datetime(2026, 8, 15, tzinfo=datetime.UTC)


@pytest.fixture
def embedder() -> FakeEmbedder:
    return FakeEmbedder()


@pytest.fixture
def haku_state(tmp_path: Path) -> HakuStateGitConfig:
    """A bare repository standing in for haku-state, with one commit on `main`."""
    origin = pygit2.init_repository(str(tmp_path / "origin.git"), bare=True, initial_head="main")
    index = pygit2.Index()
    blob = origin.create_blob(b"the egress fence keys on haku-sandbox\n")
    index.add(pygit2.IndexEntry("notes/alpha.md", blob, pygit2.enums.FileMode.BLOB))
    origin.create_commit("refs/heads/main", _AUTHOR, _AUTHOR, "seed", index.write_tree(origin), [])
    return HakuStateGitConfig(repo_url=str(tmp_path / "origin.git"), mirror_path=tmp_path / "mirror.git")


@pytest.fixture
async def operator_id(migrated_sessions: async_sessionmaker[AsyncSession]) -> UUID:
    operator_id = uuid.uuid4()
    async with migrated_sessions.begin() as session:
        session.add(Operator(operator_id=operator_id, status=OperatorStatus.ACTIVE, created_at=_NOW, updated_at=_NOW))
    return operator_id


async def say(sessions: async_sessionmaker[AsyncSession], operator_id: UUID, content: str) -> UUID:
    """One chat session holding one message, as the console would have written it."""
    session_id = uuid.uuid4()
    async with sessions.begin() as session:
        session.add(
            ClaudeChatSession(
                session_id=session_id,
                operator_id=operator_id,
                status=ChatSessionStatus.CLOSED,
                bridge_token_fingerprint=b"fingerprint",
                lease_expires_at=_NOW,
                created_at=_NOW,
                updated_at=_NOW,
            )
        )
        # Before the message, which points at it: one unit of work orders inserts by mapper, not
        # by the order they were added.
        await session.flush()
        session.add(
            ClaudeChatMessage(
                message_id=uuid.uuid4(),
                session_id=session_id,
                role=ChatMessageRole.USER,
                status=ChatMessageStatus.COMPLETE,
                content=content,
                tool_uses=[],
                created_at=_NOW,
                updated_at=_NOW,
            )
        )
    return session_id


async def test_a_chat_sweep_makes_a_session_searchable(
    migrated_engine: AsyncEngine,
    migrated_sessions: async_sessionmaker[AsyncSession],
    operator_id: UUID,
    embedder: FakeEmbedder,
) -> None:
    session_id = await say(migrated_sessions, operator_id, "we decided to keep the egress fence")

    await StateIndexMaintenance(migrated_engine, migrated_sessions, embedder=embedder, git=None).sync_chat_once()

    hits = await PostgresIndexSearcher(migrated_sessions, embedder).search(
        "egress", corpus=SearchCorpus.CONVERSATIONS, limit=5, path_prefix=None, session_id=None
    )
    assert [hit.source.session_id for hit in hits if isinstance(hit.source, ConversationSource)] == [session_id]


async def test_a_git_sweep_makes_the_tip_searchable(
    migrated_engine: AsyncEngine,
    migrated_sessions: async_sessionmaker[AsyncSession],
    haku_state: HakuStateGitConfig,
    embedder: FakeEmbedder,
) -> None:
    await StateIndexMaintenance(migrated_engine, migrated_sessions, embedder=embedder, git=haku_state).sync_git_once()

    hits = await PostgresIndexSearcher(migrated_sessions, embedder).search(
        "egress", corpus=SearchCorpus.HAKU_STATE, limit=5, path_prefix=None, session_id=None
    )
    assert [hit.source.path for hit in hits if isinstance(hit.source, HakuStateSource)] == ["notes/alpha.md"]


async def test_an_unmoved_remote_is_never_fetched(
    migrated_engine: AsyncEngine,
    migrated_sessions: async_sessionmaker[AsyncSession],
    haku_state: HakuStateGitConfig,
    embedder: FakeEmbedder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The point of the `ls-remote` gate: polling often must not mean pulling objects often."""
    maintenance = StateIndexMaintenance(migrated_engine, migrated_sessions, embedder=embedder, git=haku_state)
    await maintenance.sync_git_once()

    def never(*args: object, **kwargs: object) -> str:
        raise AssertionError("fetched a remote whose tip had not moved")

    monkeypatch.setattr(state_index_sync, "_fetch", never)
    await maintenance.sync_git_once()


async def test_status_reports_the_remote_before_anything_is_indexed(
    migrated_engine: AsyncEngine,
    migrated_sessions: async_sessionmaker[AsyncSession],
    haku_state: HakuStateGitConfig,
    embedder: FakeEmbedder,
) -> None:
    """A sweep that cannot finish still has to leave evidence that it looked."""
    with pytest.raises(RuntimeError):
        await StateIndexMaintenance(
            migrated_engine, migrated_sessions, embedder=ExplodingEmbedder(), git=haku_state
        ).sync_git_once()

    status = (await PostgresIndexSearcher(migrated_sessions, embedder).status()).haku_state
    assert status.indexed_commit is None
    assert status.remote_commit is not None
    assert status.branch == "main"


async def test_status_reports_the_indexed_commit_once_a_sync_lands(
    migrated_engine: AsyncEngine,
    migrated_sessions: async_sessionmaker[AsyncSession],
    haku_state: HakuStateGitConfig,
    embedder: FakeEmbedder,
) -> None:
    await StateIndexMaintenance(migrated_engine, migrated_sessions, embedder=embedder, git=haku_state).sync_git_once()

    status = (await PostgresIndexSearcher(migrated_sessions, embedder).status()).haku_state
    assert status.indexed_commit == status.remote_commit
    assert (status.files, status.embedded_chunks) == (1, 1)


async def test_without_git_configured_the_git_sweep_is_a_no_op(
    migrated_engine: AsyncEngine, migrated_sessions: async_sessionmaker[AsyncSession], embedder: FakeEmbedder
) -> None:
    """The console serves the chat corpus alone until it is given a way to read haku-state."""
    await StateIndexMaintenance(migrated_engine, migrated_sessions, embedder=embedder, git=None).sync_git_once()

    # Reported as empty rather than absent: a caller cannot tell "unconfigured" from "not yet
    # indexed" from the corpus's own status, and `remote_commit` is what says nothing has looked.
    status = (await PostgresIndexSearcher(migrated_sessions, embedder).status()).haku_state
    assert (status.indexed_commit, status.remote_commit, status.embedded_chunks) == (None, None, 0)


async def test_a_replica_that_loses_the_lock_leaves_the_work_alone(
    migrated_engine: AsyncEngine,
    migrated_sessions: async_sessionmaker[AsyncSession],
    operator_id: UUID,
    embedder: FakeEmbedder,
) -> None:
    """The advisory lock is what keeps two replicas from embedding the same backlog twice."""
    await say(migrated_sessions, operator_id, "the leader indexes this one")

    async with migrated_engine.connect() as leader:
        assert await leader.scalar(text("SELECT pg_try_advisory_lock(:lock)"), {"lock": CHAT_ADVISORY_LOCK})
        await StateIndexMaintenance(migrated_engine, migrated_sessions, embedder=embedder, git=None).sync_chat_once()

    assert (await PostgresIndexSearcher(migrated_sessions, embedder).status()).conversations.sessions == 0


if __name__ == "__main__":
    pytest_bazel.main()
