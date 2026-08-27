"""The chat corpus's load-bearing invariants: what re-embeds, what re-uses, what stops matching."""

from __future__ import annotations

import datetime
import uuid
from uuid import UUID

import pytest
import pytest_bazel
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from haku.console.chat_models import ItemStatus, ItemType, RuntimeKind
from haku.console.database_schema import Base as ConsoleBase, Conversation, ConversationItem, Operator, Session
from haku.console.operator_identity import OperatorStatus
from haku.recall_index.chat_sync import ChatSyncReport, sync_chat
from haku.recall_index.embedding_sync import embed_pending
from haku.recall_index.fake_embedder import FakeEmbedder
from haku.recall_index.query import query_chat
from haku.recall_index.schema import ChatChunk, ContentEmbedding, IndexType
from haku.recall_index.store import ChatSearchHit, register_index

_NOW = datetime.datetime(2026, 8, 11, tzinfo=datetime.UTC)

# `conversation_item` is the corpus; the others are the foreign keys it hangs off. `conversation`
# is one of them without holding anything the corpus reads: `sessions.conversation_id` points at
# it, so `sessions` is not creatable without it. The tiny `agents` anchor below satisfies the
# conversation's nullable provenance FK without pulling the complete enrollment schema into this
# focused corpus test. `sessions.agent_binding_id` is likewise nullable here, but PostgreSQL still
# requires its referenced table to exist when the focused sessions table is created.
_CHAT_SOURCE_TABLES = ("operators", "conversation", "sessions", "conversation_turn", "conversation_item")


@pytest.fixture
async def chat_source(session: AsyncSession) -> AsyncSession:
    """The console tables the chat corpus reads, created in the same database it indexes into.

    Only what the corpus needs, rather than the console's whole schema, which would drag
    every unrelated table into a test about chunking. Lives here rather than in `conftest.py` so
    the git corpus's tests do not take a dependency on the console to get a database — what they
    share is the `session` fixture, which resets `public` for exactly this to be creatable.
    """
    connection = await session.connection()
    await connection.execute(text("CREATE TABLE agents (agent_id UUID PRIMARY KEY)"))
    await connection.execute(text("CREATE TABLE credential_bindings (binding_id UUID PRIMARY KEY)"))
    await connection.run_sync(
        ConsoleBase.metadata.create_all, tables=[ConsoleBase.metadata.tables[name] for name in _CHAT_SOURCE_TABLES]
    )
    return session


async def new_operator(source: AsyncSession) -> UUID:
    operator_id = uuid.uuid4()
    source.add(Operator(operator_id=operator_id, status=OperatorStatus.ACTIVE, created_at=_NOW, updated_at=_NOW))
    await source.flush()
    return operator_id


async def new_session(source: AsyncSession, operator_id: UUID) -> UUID:
    session_id = uuid.uuid4()
    conversation_id = uuid.uuid4()
    source.add(
        Conversation(
            conversation_id=conversation_id,
            operator_id=operator_id,
            runtime_kind=RuntimeKind.CLAUDE_CODE,
            created_at=_NOW,
        )
    )
    # Before the session that points at it: a bare `ForeignKey` does not order the unit of work.
    await source.flush()
    source.add(
        Session(
            session_id=session_id,
            operator_id=operator_id,
            conversation_id=conversation_id,
            bridge_token_fingerprint=session_id.bytes,
            lease_expires_at=_NOW,
            ended_at=_NOW,
            created_at=_NOW,
            updated_at=_NOW,
        )
    )
    await source.flush()
    return session_id


async def say(
    source: AsyncSession,
    session_id: UUID,
    content: str,
    *,
    item_type: ItemType = ItemType.PROMPT,
    minute: int = 0,
    status: ItemStatus = ItemStatus.COMPLETE,
) -> UUID:
    item_id = uuid.uuid4()
    at = _NOW + datetime.timedelta(minutes=minute)
    conversation_id = await source.scalar(select(Session.conversation_id).where(Session.session_id == session_id))
    source.add(
        ConversationItem(
            item_id=item_id,
            conversation_id=conversation_id,
            session_id=session_id,
            item_type=item_type,
            status=status,
            opened_seq=minute * 2 + 1,
            closed_seq=minute * 2 + 2 if status is ItemStatus.COMPLETE else None,
            item_text=content,
            created_at=at,
            updated_at=at,
        )
    )
    await source.flush()
    return item_id


@pytest.fixture
async def operator_id(chat_source: AsyncSession) -> UUID:
    return await new_operator(chat_source)


# Past every test session's quiet window, so a sync indexes what it finds. The window itself is
# the subject of its own tests below.
_SETTLED = _NOW + datetime.timedelta(hours=1)
_CHAT_INDEX = "test-chat"


async def embed_all(session: AsyncSession, embedder: FakeEmbedder) -> int:
    total = 0
    while (report := await embed_pending(session, embedder=embedder)).contents_embedded:
        total += report.contents_embedded
        await session.commit()
    return total


async def run_sync(
    session: AsyncSession, embedder: FakeEmbedder, *, index_id: str = _CHAT_INDEX, now: datetime.datetime = _SETTLED
) -> ChatSyncReport:
    await register_index(session, index_id, index_type=IndexType.CHAT)
    report = await sync_chat(session, index_id=index_id, now=now)
    await session.commit()
    await embed_all(session, embedder)
    return report


async def find(
    session: AsyncSession,
    embedder: FakeEmbedder,
    query: str,
    *,
    index_id: str = _CHAT_INDEX,
    session_id: UUID | None = None,
) -> list[ChatSearchHit]:
    return await query_chat(session, embedder, query, index_id=index_id, limit=5, session_id=session_id)


async def test_a_hit_names_the_session_and_the_messages_it_holds(
    chat_source: AsyncSession, operator_id: UUID, embedder: FakeEmbedder
) -> None:
    session_id = await new_session(chat_source, operator_id)
    asked = await say(chat_source, session_id, "what happened with alpha", minute=0)
    answered = await say(chat_source, session_id, "alpha was filed", item_type=ItemType.MESSAGE, minute=1)
    await run_sync(chat_source, embedder)

    (hit,) = await find(chat_source, embedder, "alpha")
    assert hit.session_id == session_id
    assert hit.message_ids == [asked, answered]


async def test_a_second_conversation_index_has_its_own_windows(
    chat_source: AsyncSession, operator_id: UUID, embedder: FakeEmbedder
) -> None:
    session_id = await new_session(chat_source, operator_id)
    await say(chat_source, session_id, "separate conversation index", minute=0)
    await register_index(chat_source, "second-conversations", index_type=IndexType.CHAT)
    await chat_source.commit()

    await run_sync(chat_source, embedder)
    await run_sync(chat_source, embedder, index_id="second-conversations")

    assert [hit.session_id for hit in await find(chat_source, embedder, "separate")] == [session_id]
    assert [
        hit.session_id for hit in await find(chat_source, embedder, "separate", index_id="second-conversations")
    ] == [session_id]
    rows = await chat_source.execute(
        select(func.count()).select_from(ChatChunk).where(ChatChunk.session_id == session_id)
    )
    assert rows.scalar_one() == 2


async def test_only_complete_items_are_indexed(
    chat_source: AsyncSession, operator_id: UUID, embedder: FakeEmbedder
) -> None:
    session_id = await new_session(chat_source, operator_id)
    await say(chat_source, session_id, "beta is done", minute=0)
    await say(chat_source, session_id, "beta streaming", minute=1, status=ItemStatus.OPEN)
    await run_sync(chat_source, embedder)

    (hit,) = await find(chat_source, embedder, "beta")
    assert "streaming" not in hit.text


async def test_an_unchanged_session_is_not_re_indexed(
    chat_source: AsyncSession, operator_id: UUID, embedder: FakeEmbedder
) -> None:
    session_id = await new_session(chat_source, operator_id)
    await say(chat_source, session_id, "gamma", minute=0)
    await run_sync(chat_source, embedder)

    again = await run_sync(chat_source, embedder)
    assert (again.sessions_indexed, again.sessions_unchanged, again.contents_materialized) == (0, 1, 0)


async def test_a_new_message_re_windows_the_session(
    chat_source: AsyncSession, operator_id: UUID, embedder: FakeEmbedder
) -> None:
    session_id = await new_session(chat_source, operator_id)
    await say(chat_source, session_id, "delta", minute=0)
    await run_sync(chat_source, embedder)
    later = await say(chat_source, session_id, "delta and epsilon", minute=1)
    report = await run_sync(chat_source, embedder)

    assert report.sessions_indexed == 1
    (hit,) = await find(chat_source, embedder, "epsilon")
    # One window covering both messages, not the superseded single-message one beside it.
    assert hit.message_ids[-1] == later
    windows = await chat_source.execute(
        select(func.count()).select_from(ChatChunk).where(ChatChunk.session_id == session_id)
    )
    assert windows.scalar_one() == 1


async def test_the_same_exchange_in_another_session_costs_no_embedding(
    chat_source: AsyncSession, operator_id: UUID, embedder: FakeEmbedder
) -> None:
    first = await new_session(chat_source, operator_id)
    await say(chat_source, first, "zeta filing", minute=0)
    await run_sync(chat_source, embedder)

    second = await new_session(chat_source, operator_id)
    await say(chat_source, second, "zeta filing", minute=0)
    report = await run_sync(chat_source, embedder)

    assert (report.sessions_indexed, report.contents_materialized) == (1, 1)
    assert {hit.session_id for hit in await find(chat_source, embedder, "zeta")} == {first, second}


async def test_a_session_the_console_dropped_stops_matching(
    chat_source: AsyncSession, operator_id: UUID, embedder: FakeEmbedder
) -> None:
    session_id = await new_session(chat_source, operator_id)
    await say(chat_source, session_id, "eta", minute=0)
    await run_sync(chat_source, embedder)

    await chat_source.execute(delete(ConversationItem).where(ConversationItem.session_id == session_id))
    report = await run_sync(chat_source, embedder)

    assert report.sessions_forgotten == 1
    assert await find(chat_source, embedder, "eta") == []
    # The durable semantic representation stays available if the same words are said again.
    cached = await chat_source.execute(select(func.count()).select_from(ContentEmbedding))
    assert cached.scalar_one() > 0


async def test_the_session_filter_narrows_the_search(
    chat_source: AsyncSession, operator_id: UUID, embedder: FakeEmbedder
) -> None:
    first = await new_session(chat_source, operator_id)
    await say(chat_source, first, "theta here", minute=0)
    second = await new_session(chat_source, operator_id)
    await say(chat_source, second, "theta there", minute=0)
    await run_sync(chat_source, embedder)

    hits = await find(chat_source, embedder, "theta", session_id=second)
    assert [hit.session_id for hit in hits] == [second]


async def test_a_changed_model_re_embeds_without_re_syncing_messages(
    chat_source: AsyncSession, operator_id: UUID
) -> None:
    session_id = await new_session(chat_source, operator_id)
    await say(chat_source, session_id, "alpha", minute=0)
    await run_sync(chat_source, FakeEmbedder())

    successor = FakeEmbedder(model_key="fake-v2")
    report = await sync_chat(chat_source, index_id=_CHAT_INDEX, now=_SETTLED)
    assert report.sessions_unchanged == 1
    assert await embed_all(chat_source, successor) == 1
    assert len(await find(chat_source, successor, "alpha")) == 1


async def test_a_session_still_being_written_to_is_left_to_settle(
    chat_source: AsyncSession, operator_id: UUID, embedder: FakeEmbedder
) -> None:
    """Indexing mid-exchange re-windows the whole tail, and the next turn would do it again."""
    session_id = await new_session(chat_source, operator_id)
    await say(chat_source, session_id, "iota", minute=0)

    report = await run_sync(chat_source, embedder, now=_NOW + datetime.timedelta(seconds=5))

    assert (report.sessions_settling, report.sessions_indexed) == (1, 0)
    assert await find(chat_source, embedder, "iota") == []


async def test_a_settled_session_is_indexed_by_the_next_sweep(
    chat_source: AsyncSession, operator_id: UUID, embedder: FakeEmbedder
) -> None:
    """Nothing records that a session was skipped, so waiting costs only the delay."""
    session_id = await new_session(chat_source, operator_id)
    await say(chat_source, session_id, "kappa", minute=0)
    await run_sync(chat_source, embedder, now=_NOW + datetime.timedelta(seconds=5))

    report = await run_sync(chat_source, embedder)

    assert (report.sessions_settling, report.sessions_indexed) == (0, 1)
    assert [hit.session_id for hit in await find(chat_source, embedder, "kappa")] == [session_id]


if __name__ == "__main__":
    pytest_bazel.main()
