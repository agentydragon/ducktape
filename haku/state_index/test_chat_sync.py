"""The chat corpus's load-bearing invariants: what re-embeds, what re-uses, what stops matching."""

from __future__ import annotations

import datetime
import uuid
from uuid import UUID

import pytest
import pytest_bazel
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from haku.console.chat_models import ChatMessageRole, ChatMessageStatus, SessionStatus
from haku.console.database_schema import Base as ConsoleBase, Operator, Session, SessionMessage
from haku.console.operator_identity import OperatorStatus
from haku.state_index.chat_sync import ChatSyncReport, sync_chat
from haku.state_index.fake_embedder import FakeEmbedder
from haku.state_index.query import query_chat
from haku.state_index.schema import ChatChunk, Chunk, Corpus
from haku.state_index.store import ChatSearchHit

_NOW = datetime.datetime(2026, 8, 11, tzinfo=datetime.UTC)

# `session_messages` is the corpus; the other two are the foreign keys it hangs off.
_CHAT_SOURCE_TABLES = ("operators", "sessions", "session_messages")


@pytest.fixture
async def chat_source(session: AsyncSession) -> AsyncSession:
    """The console tables the chat corpus reads, created in the same database it indexes into.

    Only the three the corpus needs, rather than the console's whole schema, which would drag
    every unrelated table into a test about chunking. Lives here rather than in `conftest.py` so
    the git corpus's tests do not take a dependency on the console to get a database — what they
    share is the `session` fixture, which resets `public` for exactly this to be creatable.
    """
    connection = await session.connection()
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
    source.add(
        Session(
            session_id=session_id,
            operator_id=operator_id,
            status=SessionStatus.CLOSED,
            bridge_token_fingerprint=b"fingerprint",
            lease_expires_at=_NOW,
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
    role: ChatMessageRole = ChatMessageRole.USER,
    minute: int = 0,
    status: ChatMessageStatus = ChatMessageStatus.COMPLETE,
) -> UUID:
    message_id = uuid.uuid4()
    at = _NOW + datetime.timedelta(minutes=minute)
    source.add(
        SessionMessage(
            message_id=message_id,
            session_id=session_id,
            role=role,
            status=status,
            content=content,
            created_at=at,
            updated_at=at,
        )
    )
    await source.flush()
    return message_id


@pytest.fixture
async def operator_id(chat_source: AsyncSession) -> UUID:
    return await new_operator(chat_source)


# Past every test session's quiet window, so a sync indexes what it finds. The window itself is
# the subject of its own tests below.
_SETTLED = _NOW + datetime.timedelta(hours=1)


async def run_sync(
    session: AsyncSession, embedder: FakeEmbedder, *, now: datetime.datetime = _SETTLED
) -> ChatSyncReport:
    report = await sync_chat(session, embedder=embedder, now=now)
    await session.commit()
    return report


async def find(
    session: AsyncSession, embedder: FakeEmbedder, query: str, *, session_id: UUID | None = None
) -> list[ChatSearchHit]:
    return await query_chat(session, embedder, query, limit=5, session_id=session_id)


async def test_a_hit_names_the_session_and_the_messages_it_holds(
    chat_source: AsyncSession, operator_id: UUID, embedder: FakeEmbedder
) -> None:
    session_id = await new_session(chat_source, operator_id)
    asked = await say(chat_source, session_id, "what happened with alpha", minute=0)
    answered = await say(chat_source, session_id, "alpha was filed", role=ChatMessageRole.ASSISTANT, minute=1)
    await run_sync(chat_source, embedder)

    (hit,) = await find(chat_source, embedder, "alpha")
    assert hit.session_id == session_id
    assert hit.message_ids == [asked, answered]


async def test_only_complete_messages_are_indexed(
    chat_source: AsyncSession, operator_id: UUID, embedder: FakeEmbedder
) -> None:
    session_id = await new_session(chat_source, operator_id)
    await say(chat_source, session_id, "beta is done", minute=0)
    await say(chat_source, session_id, "beta streaming", minute=1, status=ChatMessageStatus.STREAMING)
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
    assert (again.sessions_indexed, again.sessions_unchanged, again.windows_embedded) == (0, 1, 0)


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

    assert (report.sessions_indexed, report.windows_embedded, report.windows_reused) == (1, 0, 1)
    assert {hit.session_id for hit in await find(chat_source, embedder, "zeta")} == {first, second}


async def test_a_session_the_console_dropped_stops_matching(
    chat_source: AsyncSession, operator_id: UUID, embedder: FakeEmbedder
) -> None:
    session_id = await new_session(chat_source, operator_id)
    await say(chat_source, session_id, "eta", minute=0)
    await run_sync(chat_source, embedder)

    await chat_source.execute(delete(SessionMessage).where(SessionMessage.session_id == session_id))
    report = await run_sync(chat_source, embedder)

    assert report.sessions_forgotten == 1
    assert await find(chat_source, embedder, "eta") == []
    # The embedding stays cached against the day the same words are said again.
    cached = await chat_source.execute(select(func.count()).select_from(Chunk).where(Chunk.corpus == Corpus.CHAT))
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


async def test_a_changed_model_re_embeds_the_same_messages(chat_source: AsyncSession, operator_id: UUID) -> None:
    session_id = await new_session(chat_source, operator_id)
    await say(chat_source, session_id, "alpha", minute=0)
    await run_sync(chat_source, FakeEmbedder())

    successor = FakeEmbedder(model_key="fake-v2")
    report = await run_sync(chat_source, successor)
    assert (report.sessions_indexed, report.windows_embedded) == (1, 1)
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
