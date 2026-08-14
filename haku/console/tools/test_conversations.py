"""Tests for the in-process `haku_conversations` MCP server (build_mcp)."""

from __future__ import annotations

import datetime
from collections.abc import Sequence

import pytest_bazel
from fastmcp import Client

from haku.console.tools.conversations import (
    HAKU_CONVERSATIONS_SERVER_ID,
    MAX_FRAME_BYTES,
    Conversation,
    RolloutFrame,
    TurnRecord,
    build_mcp,
)

NOW = datetime.datetime(2026, 8, 12, 9, 0, tzinfo=datetime.UTC)


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

    async def list_conversations(self, *, limit: int) -> list[Conversation]:
        return [
            Conversation(
                session_id="11111111-1111-1111-1111-111111111111",
                surface="matrix",
                room_id="!room:example.org",
                status="closed",
                created_at=NOW,
            )
        ][:limit]

    async def read_frames(
        self, session_id: str, *, after_seq: int | None, limit: int, kinds: Sequence[str] | None
    ) -> list[RolloutFrame]:
        self.queries.append({"session_id": session_id, "after_seq": after_seq, "limit": limit, "kinds": kinds})
        selected = [frame for frame in self._frames if kinds is None or frame.kind in kinds]
        if after_seq is not None:
            selected = [frame for frame in selected if frame.frame_seq > after_seq]
        return selected[:limit]

    async def list_turns(self, session_id: str, *, limit: int) -> list[TurnRecord]:
        return [
            TurnRecord(
                turn_id="22222222-2222-2222-2222-222222222222",
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

    assert tools == {"list_conversations", "read_rollout", "list_turns"}
    assert HAKU_CONVERSATIONS_SERVER_ID == "haku_conversations"


async def test_a_conversation_says_which_room_it_served() -> None:
    async with Client(build_mcp(_Reader())) as client:
        result = await client.call_tool("list_conversations", {})

    assert not result.is_error
    assert result.data[0].room_id == "!room:example.org"


async def test_a_full_page_carries_the_cursor_for_the_next_one() -> None:
    reader = _Reader(*(_frame(seq) for seq in (1, 2, 3, 4)))

    async with Client(build_mcp(reader)) as client:
        result = await client.call_tool("read_rollout", {"session_id": "s", "limit": 2})

    assert [frame.frame_seq for frame in result.data.frames] == [1, 2]
    assert result.data.next_after_seq == 2


async def test_a_short_page_is_the_last_one() -> None:
    """Otherwise a reader pages forever, asking for frames that do not exist."""
    reader = _Reader(_frame(1), _frame(2))

    async with Client(build_mcp(reader)) as client:
        result = await client.call_tool("read_rollout", {"session_id": "s", "limit": 25})

    assert result.data.next_after_seq is None


async def test_the_cursor_reaches_the_store_rather_than_being_filtered_here() -> None:
    """Paging has to happen in the query; filtering a page after the fact would return
    fewer rows than asked for and read as the end of the log."""
    reader = _Reader(*(_frame(seq) for seq in (1, 2, 3)))

    async with Client(build_mcp(reader)) as client:
        await client.call_tool("read_rollout", {"session_id": "s", "after_seq": 1, "kinds": ["assistant"]})

    assert reader.queries == [{"session_id": "s", "after_seq": 1, "limit": 25, "kinds": ["assistant"]}]


async def test_an_oversized_frame_is_clipped_and_says_so() -> None:
    """A row limit alone does not bound a response: one tool result can be a whole file."""
    big = _frame(1, payload={"type": "user", "content": "x" * (MAX_FRAME_BYTES * 2)})
    reader = _Reader(big, _frame(2))

    async with Client(build_mcp(reader)) as client:
        result = await client.call_tool("read_rollout", {"session_id": "s"})

    clipped, kept = result.data.frames
    assert clipped.payload is None
    assert clipped.clipped_bytes > MAX_FRAME_BYTES
    assert kept.payload == {"type": "assistant"}


async def test_a_turn_carries_the_range_to_read_and_what_it_cost() -> None:
    """The point of listing exchanges is to pick one and then read its frames, so the bracket has
    to come back with the accounting rather than instead of it."""
    async with Client(build_mcp(_Reader())) as client:
        result = await client.call_tool("list_turns", {"session_id": "s"})

    [turn] = result.data
    assert (turn.first_frame_seq, turn.last_frame_seq) == (1, 4)
    assert turn.outcome == "answered"
    assert turn.cost_usd == 0.0125


async def test_a_page_size_above_the_cap_is_refused() -> None:
    """The cap is the only thing keeping a read from being a dump."""
    async with Client(build_mcp(_Reader())) as client:
        result = await client.call_tool("read_rollout", {"session_id": "s", "limit": 10_000}, raise_on_error=False)

    assert result.is_error


if __name__ == "__main__":
    pytest_bazel.main()
