"""Tests for the in-process `haku_index` MCP server (build_mcp).

What matters here is the contract with the caller: a hit is a pointer, and it carries everything
needed to resolve it somewhere else. A haku-state hit that named a path without the commit it is
at would look fine in a schema and send a reader to whatever that path holds today.
"""

from __future__ import annotations

import datetime
from uuid import UUID

import pytest_bazel
from fastmcp import Client

from haku.console.tools.state_index import (
    HAKU_INDEX_SERVER_ID,
    ConversationSource,
    ConversationsStatus,
    HakuStateSource,
    HakuStateStatus,
    IndexStatus,
    SearchCorpus,
    SearchHit,
    build_mcp,
)

NOW = datetime.datetime(2026, 8, 14, 9, 0, tzinfo=datetime.UTC)
SESSION = UUID("11111111-1111-1111-1111-111111111111")
MESSAGES = [UUID("22222222-2222-2222-2222-222222222222"), UUID("33333333-3333-3333-3333-333333333333")]


def _haku_state(score: float) -> SearchHit:
    return SearchHit(
        score=score,
        snippet="how to file an intake item",
        source=HakuStateSource(
            path="notes/intake.md", commit_sha="deadbeef", blob_sha="cafe1234", byte_start=0, byte_end=40
        ),
    )


def _conversation(score: float) -> SearchHit:
    return SearchHit(
        score=score,
        snippet="user: what about the egress fence",
        source=ConversationSource(
            session_id=SESSION,
            room_id="!room:allegedly.works",
            message_ids=MESSAGES,
            first_message_at=NOW,
            last_message_at=NOW,
        ),
    )


class _Searcher:
    """An `IndexSearcher` over fixed answers, recording how it was queried."""

    def __init__(self, *hits: SearchHit) -> None:
        self.hits = list(hits)
        self.queries: list[dict] = []

    async def search(
        self, query: str, *, corpus: SearchCorpus, limit: int, path_prefix: str | None, session_id: UUID | None
    ) -> list[SearchHit]:
        self.queries.append(
            {"query": query, "corpus": corpus, "limit": limit, "path_prefix": path_prefix, "session_id": session_id}
        )
        return self.hits

    async def status(self) -> IndexStatus:
        return IndexStatus(
            haku_state=HakuStateStatus(
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


# `result.data` rebuilds the hit model, but leaves `source` as the mapping it was serialized to:
# FastMCP reconstructs from the output schema, where the discriminated union is an `anyOf`.
async def test_a_haku_state_hit_carries_what_it_takes_to_read_the_file() -> None:
    async with Client(build_mcp(_Searcher(_haku_state(0.9)))) as client:
        (hit,) = (await client.call_tool("search", {"query": "intake"})).data
    # Path, the commit it is at, and the blob itself: a clone can resolve any of the three.
    assert (hit.source["path"], hit.source["commit_sha"], hit.source["blob_sha"]) == (
        "notes/intake.md",
        "deadbeef",
        "cafe1234",
    )


async def test_a_conversation_hit_names_the_session_the_room_and_its_messages() -> None:
    async with Client(build_mcp(_Searcher(_conversation(0.8)))) as client:
        (hit,) = (await client.call_tool("search", {"query": "egress fence"})).data
    assert (hit.source["session_id"], hit.source["room_id"]) == (str(SESSION), "!room:allegedly.works")
    assert hit.source["message_ids"] == [str(message_id) for message_id in MESSAGES]


async def test_both_corpora_come_back_from_one_call_and_say_where_each_is_from() -> None:
    async with Client(build_mcp(_Searcher(_haku_state(0.9), _conversation(0.8)))) as client:
        hits = (await client.call_tool("search", {"query": "intake"})).data
    # Score and snippet are the same shape either way; only the provenance discriminates.
    assert [hit.source["kind"] for hit in hits] == ["haku_state", "conversation"]
    assert [hit.score for hit in hits] == [0.9, 0.8]


async def test_searching_defaults_to_both_corpora() -> None:
    searcher = _Searcher()
    async with Client(build_mcp(searcher)) as client:
        await client.call_tool("search", {"query": "intake"})
    assert searcher.queries[-1]["corpus"] is SearchCorpus.ALL


async def test_the_corpus_and_session_filters_reach_the_searcher() -> None:
    searcher = _Searcher()
    async with Client(build_mcp(searcher)) as client:
        await client.call_tool("search", {"query": "intake", "corpus": "conversations", "session_id": str(SESSION)})
    query = searcher.queries[-1]
    assert (query["corpus"], query["session_id"]) == (SearchCorpus.CONVERSATIONS, SESSION)


async def test_status_reports_the_backlog_an_agent_needs_to_read_an_empty_result() -> None:
    async with Client(build_mcp(_Searcher())) as client:
        status = (await client.call_tool("index_status", {})).data
    assert status.conversations.unindexed_messages == 4
    assert status.haku_state.commit_sha == "abc123"


def test_the_server_is_named_for_its_id() -> None:
    assert build_mcp(_Searcher()).name == HAKU_INDEX_SERVER_ID


if __name__ == "__main__":
    pytest_bazel.main()
