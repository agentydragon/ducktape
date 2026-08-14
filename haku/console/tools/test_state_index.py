"""Tests for the in-process `haku_index` MCP server (build_mcp).

What matters here is the contract with the caller: search hands back a pointer, and the pointer
resolves. A hit that named a path without the blob sha, or messages the read tool would not take,
would still look fine in a schema and be useless in a session.
"""

from __future__ import annotations

import datetime
from uuid import UUID

import pytest
import pytest_bazel
from fastmcp import Client

from haku.console.tools.state_index import (
    HAKU_INDEX_SERVER_ID,
    ChatMessage,
    ConversationHit,
    ConversationsStatus,
    IndexStatus,
    NoteHit,
    NoteSearchResult,
    NotesStatus,
    build_mcp,
)

NOW = datetime.datetime(2026, 8, 14, 9, 0, tzinfo=datetime.UTC)
SESSION = UUID("11111111-1111-1111-1111-111111111111")
MESSAGES = [UUID("22222222-2222-2222-2222-222222222222"), UUID("33333333-3333-3333-3333-333333333333")]


class _Searcher:
    """An `IndexSearcher` over fixed answers, recording how it was queried."""

    def __init__(self, *, notes: NoteSearchResult | None = None, note_text: str | None = "note body") -> None:
        self.notes = notes
        self.note_text = note_text
        self.queries: list[dict] = []

    async def search_notes(self, query: str, *, limit: int, path_prefix: str | None) -> NoteSearchResult | None:
        self.queries.append({"query": query, "limit": limit, "path_prefix": path_prefix})
        return self.notes

    async def search_conversations(self, query: str, *, limit: int, session_id: UUID | None) -> list[ConversationHit]:
        self.queries.append({"query": query, "limit": limit, "session_id": session_id})
        return [
            ConversationHit(
                session_id=str(SESSION),
                room_id="!room:allegedly.works",
                message_ids=[str(message_id) for message_id in MESSAGES],
                first_message_at=NOW,
                last_message_at=NOW,
                snippet="user: what about the egress fence",
                score=0.8,
            )
        ]

    async def read_note(self, path: str) -> str | None:
        self.queries.append({"path": path})
        return self.note_text

    async def read_messages(self, message_ids: list[UUID]) -> list[ChatMessage]:
        self.queries.append({"message_ids": message_ids})
        return [
            ChatMessage(
                message_id=str(message_id),
                session_id=str(SESSION),
                role="user",
                content=f"body of {message_id}",
                created_at=NOW,
            )
            for message_id in message_ids
        ]

    async def status(self) -> IndexStatus:
        return IndexStatus(
            notes=NotesStatus(
                commit_sha="abc123", branch="main", indexed_at=NOW, files=12, chunks=40, superseded_chunks=0
            ),
            conversations=ConversationsStatus(
                sessions=3,
                chunks=9,
                stale_sessions=1,
                unindexed_messages=4,
                lag_seconds=120.0,
                last_indexed_at=NOW,
                superseded_chunks=0,
            ),
        )


async def test_a_note_hit_carries_the_blob_sha_and_the_commit_it_is_at() -> None:
    searcher = _Searcher(
        notes=NoteSearchResult(
            commit_sha="deadbeef",
            branch="main",
            indexed_at=NOW,
            hits=[
                NoteHit(path="notes/intake.md", blob_sha="cafe1234", byte_start=0, byte_end=40, snippet="…", score=0.9)
            ],
        )
    )
    async with Client(build_mcp(searcher)) as client:
        result = await client.call_tool("search_notes", {"query": "intake"})
    assert result.data.commit_sha == "deadbeef"
    assert result.data.hits[0].blob_sha == "cafe1234"


async def test_searching_an_empty_notes_index_says_so_rather_than_returning_nothing() -> None:
    """An empty list would read as "not written down anywhere", which is a different claim."""
    async with Client(build_mcp(_Searcher(notes=None))) as client:
        with pytest.raises(Exception, match="empty"):
            await client.call_tool("search_notes", {"query": "intake"})


async def test_a_conversation_hit_names_the_room_and_the_messages_to_read() -> None:
    async with Client(build_mcp(_Searcher())) as client:
        hit = (await client.call_tool("search_conversations", {"query": "egress fence"})).data[0]
        assert hit.room_id == "!room:allegedly.works"
        # The ids a hit hands back are exactly what the read tool takes.
        read = await client.call_tool("read_messages", {"message_ids": hit.message_ids})
    assert [message.message_id for message in read.data] == [str(message_id) for message_id in MESSAGES]


async def test_the_session_filter_reaches_the_searcher_as_a_uuid() -> None:
    searcher = _Searcher()
    async with Client(build_mcp(searcher)) as client:
        await client.call_tool("search_conversations", {"query": "intake", "session_id": str(SESSION)})
    assert searcher.queries[-1]["session_id"] == SESSION


async def test_reading_a_note_that_is_not_indexed_fails_loudly() -> None:
    async with Client(build_mcp(_Searcher(note_text=None))) as client:
        with pytest.raises(Exception, match="not in the index"):
            await client.call_tool("read_note", {"path": "notes/gone.md"})


async def test_status_reports_the_backlog_the_agent_needs_to_read_an_empty_result() -> None:
    async with Client(build_mcp(_Searcher())) as client:
        status = (await client.call_tool("index_status", {})).data
    assert status.conversations.unindexed_messages == 4
    assert status.notes.commit_sha == "abc123"


def test_the_server_is_named_for_its_id() -> None:
    assert build_mcp(_Searcher()).name == HAKU_INDEX_SERVER_ID


if __name__ == "__main__":
    pytest_bazel.main()
