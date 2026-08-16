"""Tests for the in-process `haku_conversations` MCP server (build_mcp)."""

from __future__ import annotations

import datetime
from collections.abc import Sequence
from uuid import UUID

import pytest_bazel
from fastmcp import Client

from haku.console.tools.conversations import (
    HAKU_CONVERSATIONS_SERVER_ID,
    MAX_PAGE_BYTES,
    Conversation,
    ConversationCursor,
    ConversationPage,
    RolloutFrame,
    TurnRecord,
    build_mcp,
)

SESSION = UUID("11111111-1111-1111-1111-111111111111")
OLDER_SESSION = UUID("33333333-3333-3333-3333-333333333333")
TURN = UUID("22222222-2222-2222-2222-222222222222")
NOW = datetime.datetime(2026, 8, 12, 9, 0, tzinfo=datetime.UTC)


def _big_frame(seq: int) -> RolloutFrame:
    """Two of these overrun one page's budget; one does not."""
    return _frame(seq, kind="user", payload={"type": "user", "content": "x" * (MAX_PAGE_BYTES * 2 // 3)})


def _conversation(session_id: UUID, created_at: datetime.datetime) -> Conversation:
    return Conversation(
        session_id=session_id, surface="matrix", room_id="!room:example.org", status="closed", created_at=created_at
    )


def _frame(seq: int, kind: str = "assistant", payload: dict | None = None) -> RolloutFrame:
    return RolloutFrame(
        frame_seq=seq,
        direction="from_agent",
        kind=kind,
        created_at=NOW,
        payload=payload if payload is not None else {"type": kind},
        partial=False,
    )


class _Reader:
    """A `RolloutReader` over a list, recording how it was queried."""

    def __init__(self, *frames: RolloutFrame):
        self._frames = list(frames)
        self.queries: list[dict] = []
        self.conversation_cursors: list[ConversationCursor | None] = []
        # Newest first, the order the store lists them in.
        self._conversations = [
            _conversation(SESSION, NOW),
            _conversation(OLDER_SESSION, NOW - datetime.timedelta(hours=1)),
        ]

    async def list_conversations(self, *, after: ConversationCursor | None, limit: int) -> ConversationPage:
        self.conversation_cursors.append(after)
        selected = [
            conversation
            for conversation in self._conversations
            if after is None
            or (conversation.created_at, conversation.session_id) < (after.created_at, after.session_id)
        ]
        page = selected[:limit]
        return ConversationPage(
            conversations=page, next_cursor=ConversationCursor.of(page[-1]) if len(selected) > limit else None
        )

    async def read_frames(
        self, session_id: UUID, *, after_seq: int | None, limit: int, kinds: Sequence[str] | None
    ) -> list[RolloutFrame]:
        self.queries.append({"session_id": session_id, "after_seq": after_seq, "limit": limit, "kinds": kinds})
        selected = [frame for frame in self._frames if kinds is None or frame.kind in kinds]
        if after_seq is not None:
            selected = [frame for frame in selected if frame.frame_seq > after_seq]
        return selected[:limit]

    async def list_turns(self, session_id: UUID, *, limit: int) -> list[TurnRecord]:
        return [
            TurnRecord(
                turn_id=TURN,
                first_frame_seq=1,
                last_frame_seq=4,
                started_at=NOW,
                ended_at=NOW,
                outcome="answered",
                cost_usd=0.0125,
                duration_ms=4200,
                usage={"output_tokens": 91},
            )
        ][:limit]


async def test_tool_surface() -> None:
    async with Client(build_mcp(_Reader())) as client:
        tools = {tool.name for tool in await client.list_tools()}

    assert tools == {"list_conversations", "read_rollout", "read_frame", "list_turns"}
    assert HAKU_CONVERSATIONS_SERVER_ID == "haku_conversations"


async def test_a_conversation_says_which_room_it_served() -> None:
    async with Client(build_mcp(_Reader())) as client:
        result = await client.call_tool("list_conversations", {})

    assert not result.is_error
    assert result.data.conversations[0].room_id == "!room:example.org"


async def test_a_full_page_of_sessions_names_both_halves_of_the_key_in_its_cursor() -> None:
    """`created_at` alone does not order the corpus — two sessions can start in one instant — so
    the cursor has to carry the tiebreak rather than pretend one column suffices."""
    reader = _Reader()

    async with Client(build_mcp(reader)) as client:
        result = await client.call_tool("list_conversations", {"limit": 1})

    [newest] = result.data.conversations
    assert result.data.next_cursor.created_at == newest.created_at
    assert result.data.next_cursor.session_id == newest.session_id


async def test_the_session_cursor_reaches_the_store_and_the_last_page_offers_none() -> None:
    """Paging belongs in the query: filtering a page here would return fewer rows than asked for
    and read as the end of the corpus."""
    reader = _Reader()
    cursor = ConversationCursor.of(_conversation(SESSION, NOW))

    async with Client(build_mcp(reader)) as client:
        result = await client.call_tool("list_conversations", {"limit": 1, "after": cursor.model_dump(mode="json")})

    assert reader.conversation_cursors == [cursor]
    # `str`, because `result.data` is rebuilt from the advertised JSON Schema, where a `UUID` is a
    # string with `format: uuid`. The reader above was handed the real `UUID`.
    assert [conversation.session_id for conversation in result.data.conversations] == [str(OLDER_SESSION)]
    assert result.data.next_cursor is None


async def test_a_full_page_carries_the_cursor_for_the_next_one() -> None:
    reader = _Reader(*(_frame(seq) for seq in (1, 2, 3, 4)))

    async with Client(build_mcp(reader)) as client:
        result = await client.call_tool("read_rollout", {"session_id": str(SESSION), "limit": 2})

    assert [frame.frame_seq for frame in result.data.frames] == [1, 2]
    assert result.data.next_after_seq == 2


async def test_a_short_page_is_the_last_one() -> None:
    """Otherwise a reader pages forever, asking for frames that do not exist."""
    reader = _Reader(_frame(1), _frame(2))

    async with Client(build_mcp(reader)) as client:
        result = await client.call_tool("read_rollout", {"session_id": str(SESSION), "limit": 25})

    assert result.data.next_after_seq is None


async def test_the_cursor_reaches_the_store_rather_than_being_filtered_here() -> None:
    """Paging has to happen in the query; filtering a page after the fact would return
    fewer rows than asked for and read as the end of the log."""
    reader = _Reader(*(_frame(seq) for seq in (1, 2, 3)))

    async with Client(build_mcp(reader)) as client:
        await client.call_tool("read_rollout", {"session_id": str(SESSION), "after_seq": 1, "kinds": ["assistant"]})

    # 26 rather than 25: the extra row is how the page tells "exactly full" from "more to come".
    assert reader.queries == [{"session_id": SESSION, "after_seq": 1, "limit": 26, "kinds": ["assistant"]}]


async def test_a_page_stops_on_its_byte_budget_and_says_where() -> None:
    """A row limit alone does not bound a response: one tool result can be a whole file. The
    frame that would overrun starts the next page rather than being dropped from this one."""
    reader = _Reader(_big_frame(1), _big_frame(2), _frame(3))

    async with Client(build_mcp(reader)) as client:
        result = await client.call_tool("read_rollout", {"session_id": str(SESSION), "limit": 25})

    assert [frame.frame_seq for frame in result.data.frames] == [1]
    assert result.data.next_after_seq == 1, "the overrunning frame is where the reader resumes"
    assert result.data.frames[0].clipped_bytes is None, "a frame that fits is never clipped"


async def test_a_frame_larger_than_a_whole_page_is_clipped_rather_than_wedging_the_cursor() -> None:
    """Skipping it would leave the cursor unable to advance past it, and a reader looping on the
    same page forever. It goes out with its size instead, for `read_frame` to fetch."""
    reader = _Reader(_frame(1, payload={"type": "user", "content": "x" * (MAX_PAGE_BYTES * 2)}), _frame(2))

    async with Client(build_mcp(reader)) as client:
        result = await client.call_tool("read_rollout", {"session_id": str(SESSION), "limit": 25})

    [only] = result.data.frames
    assert only.payload is None
    assert only.clipped_bytes > MAX_PAGE_BYTES
    assert result.data.next_after_seq == 1


async def test_one_named_frame_comes_back_whole_however_large() -> None:
    """The escape hatch: a page has a budget to spend, and a single named frame is the response."""
    big = _frame(1, payload={"type": "user", "content": "x" * (MAX_PAGE_BYTES * 2)})
    reader = _Reader(big, _frame(2))

    async with Client(build_mcp(reader)) as client:
        result = await client.call_tool("read_frame", {"session_id": str(SESSION), "frame_seq": 1})

    assert result.data.payload == big.payload
    assert result.data.clipped_bytes is None


async def test_a_named_frame_is_read_including_the_kinds_a_page_leaves_out() -> None:
    """`read_rollout`'s default view drops deltas, and a caller that named a `frame_seq` has
    already chosen its row — so the filter must not decide for it."""
    reader = _Reader(_frame(7, kind="stream_event"))

    async with Client(build_mcp(reader)) as client:
        result = await client.call_tool("read_frame", {"session_id": str(SESSION), "frame_seq": 7})

    assert result.data.kind == "stream_event"


async def test_a_frame_seq_that_does_not_exist_is_an_error_not_the_next_frame() -> None:
    """The cursor is exclusive, so a naive read of "after 4" answers a request for frame 5 with
    frame 6 — the wrong frame, indistinguishable from the right one."""
    reader = _Reader(_frame(4), _frame(6))

    async with Client(build_mcp(reader)) as client:
        result = await client.call_tool(
            "read_frame", {"session_id": str(SESSION), "frame_seq": 5}, raise_on_error=False
        )

    assert result.is_error


async def test_a_turn_carries_the_range_to_read_and_what_it_cost() -> None:
    """The point of listing exchanges is to pick one and then read its frames, so the bracket has
    to come back with the accounting rather than instead of it."""
    async with Client(build_mcp(_Reader())) as client:
        result = await client.call_tool("list_turns", {"session_id": str(SESSION)})

    [turn] = result.data
    assert (turn.first_frame_seq, turn.last_frame_seq) == (1, 4)
    assert turn.outcome == "answered"
    assert turn.cost_usd == 0.0125


async def test_a_page_size_above_the_cap_is_refused() -> None:
    """The cap is the only thing keeping a read from being a dump."""
    async with Client(build_mcp(_Reader())) as client:
        result = await client.call_tool(
            "read_rollout", {"session_id": str(SESSION), "limit": 10_000}, raise_on_error=False
        )

    assert result.is_error


async def test_a_session_id_that_is_not_an_id_is_refused_here() -> None:
    """The parameter is a `UUID`, so the schema refuses this before any code runs and the store
    is never handed something it would have to validate."""
    reader = _Reader()
    async with Client(build_mcp(reader)) as client:
        result = await client.call_tool("read_rollout", {"session_id": "not-an-id"}, raise_on_error=False)

    assert result.is_error
    assert reader.queries == []


if __name__ == "__main__":
    pytest_bazel.main()
