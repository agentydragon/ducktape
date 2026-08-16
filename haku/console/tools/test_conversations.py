"""Tests for the in-process `haku_conversations` MCP server (build_mcp)."""

from __future__ import annotations

import datetime
from collections.abc import Sequence
from uuid import UUID

import pytest_bazel
from fastmcp import Client
from fastmcp.client.client import CallToolResult
from more_itertools import one

from haku.console.tools.conversations import (
    HAKU_CONVERSATIONS_SERVER_ID,
    MAX_PAGE_BYTES,
    ConversationPage,
    TranscriptPage,
    build_mcp,
)
from haku.console.x.conversation_records import (
    Conversation,
    ConversationCursor,
    FrameCursor,
    FromFrames,
    MessageEntry,
    MessageRef,
    Outcome,
    ResultText,
    RolloutFrame,
    ToolResultEntry,
    TranscriptCursor,
    TranscriptEntry,
    TranscriptSlice,
    TurnCursor,
    TurnRecord,
    TurnUsage,
)

SESSION = UUID("11111111-1111-1111-1111-111111111111")
OLDER_SESSION = UUID("33333333-3333-3333-3333-333333333333")
TURN = UUID("22222222-2222-2222-2222-222222222222")
NOW = datetime.datetime(2026, 8, 12, 9, 0, tzinfo=datetime.UTC)

# Every tool that pages, and how a page of it is asked for. The point of the surface is that this
# list can be walked with one loop, so the tests below walk it.
PAGED_TOOLS: tuple[tuple[str, dict[str, str]], ...] = (
    ("list_conversations", {}),
    ("list_turns", {"session_id": str(SESSION)}),
    ("read_transcript", {"session_id": str(SESSION)}),
    ("read_rollout", {"session_id": str(SESSION)}),
)


def _transcript(result: CallToolResult) -> TranscriptPage:
    """The page as its own declared model, which also checks that the wire round-trips into it.

    `result.data` reconstructs a page from the generated schema and leaves a discriminated union's
    members as plain dicts, so an entry read off it is untyped either way.
    """
    return TranscriptPage.model_validate(result.structured_content)


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


def _message(index: int, *, first_frame_seq: int, last_frame_seq: int | None = None) -> MessageEntry:
    return MessageEntry(
        index=index,
        provenance=FromFrames(first_frame_seq=first_frame_seq, last_frame_seq=last_frame_seq or first_frame_seq),
        message=MessageRef(opened_at_frame_seq=first_frame_seq),
        text=f"answer {index}",
        agent_message_id=f"msg_{index}",
    )


def _tool_result(index: int, *, structured: object) -> ToolResultEntry:
    return ToolResultEntry(
        index=index,
        provenance=FromFrames(first_frame_seq=index + 1, last_frame_seq=index + 1),
        call_id=f"toolu_{index}",
        content=ResultText(text="ok"),
        structured=structured,
        outcome=Outcome.UNKNOWN,
    )


class _Reader:
    """A `ConversationReader` over lists, recording how it was queried."""

    def __init__(self, *frames: RolloutFrame, transcript: Sequence[TranscriptEntry] = ()):
        self._frames = list(frames)
        self._transcript = list(transcript)
        self.queries: list[dict] = []
        self.conversation_cursors: list[ConversationCursor | None] = []
        # Newest first, the order the store lists them in.
        self._conversations = [
            _conversation(SESSION, NOW),
            _conversation(OLDER_SESSION, NOW - datetime.timedelta(hours=1)),
        ]

    async def list_conversations(self, *, cursor: ConversationCursor | None, limit: int) -> list[Conversation]:
        self.conversation_cursors.append(cursor)
        return [
            conversation
            for conversation in self._conversations
            if cursor is None
            or (conversation.created_at, conversation.session_id) <= (cursor.created_at, cursor.session_id)
        ][:limit]

    async def read_frames(
        self, session_id: UUID, *, cursor: FrameCursor | None, limit: int, kinds: Sequence[str] | None
    ) -> list[RolloutFrame]:
        self.queries.append({"session_id": session_id, "cursor": cursor, "limit": limit, "kinds": kinds})
        selected = [frame for frame in self._frames if kinds is None or frame.kind in kinds]
        if cursor is not None:
            selected = [frame for frame in selected if frame.frame_seq >= cursor.frame_seq]
        return selected[:limit]

    async def list_turns(self, session_id: UUID, *, cursor: TurnCursor | None, limit: int) -> list[TurnRecord]:
        self.queries.append({"session_id": session_id, "cursor": cursor, "limit": limit})
        return [
            TurnRecord(
                turn_id=TURN,
                first_frame_seq=1,
                last_frame_seq=4,
                started_at=NOW,
                ended_at=NOW,
                outcome="answered",
                usage=TurnUsage(
                    input_tokens=12, output_tokens=91, cached_input_tokens=640, cost_usd=0.0125, duration_ms=4200
                ),
            )
        ][:limit]

    async def read_transcript(
        self, session_id: UUID, *, cursor: TranscriptCursor | None, limit: int
    ) -> TranscriptSlice:
        self.queries.append({"session_id": session_id, "cursor": cursor, "limit": limit})
        start = cursor.index if cursor is not None else 0
        return TranscriptSlice(entries=self._transcript[start : start + limit], unreadable=None)


async def test_tool_surface() -> None:
    async with Client(build_mcp(_Reader())) as client:
        tools = {tool.name for tool in await client.list_tools()}

    assert tools == {"list_conversations", "list_turns", "read_transcript", "read_rollout", "read_frame"}
    assert HAKU_CONVERSATIONS_SERVER_ID == "haku_conversations"


async def test_every_listing_answers_in_the_same_shape() -> None:
    """The point of the surface: one loop reads any of them. A tool that grew its own envelope
    would pass its own test and still break that."""
    reader = _Reader(_frame(1), transcript=[_message(0, first_frame_seq=1)])

    async with Client(build_mcp(reader)) as client:
        for tool, arguments in PAGED_TOOLS:
            result = await client.call_tool(tool, arguments)
            assert result.data.items, tool
            assert hasattr(result.data, "next_cursor"), tool


async def test_a_conversation_says_which_room_it_served() -> None:
    async with Client(build_mcp(_Reader())) as client:
        result = await client.call_tool("list_conversations", {})

    assert not result.is_error
    assert result.data.items[0].room_id == "!room:example.org"


async def test_a_full_page_of_sessions_names_both_halves_of_the_key_in_its_cursor() -> None:
    """`created_at` alone does not order the corpus — two sessions can start in one instant — so
    the cursor has to carry the tiebreak rather than pretend one column suffices."""
    async with Client(build_mcp(_Reader())) as client:
        result = await client.call_tool("list_conversations", {"limit": 1})

    page = ConversationPage.model_validate(result.structured_content)
    assert [conversation.session_id for conversation in page.items] == [SESSION]
    assert page.next_cursor == ConversationCursor(
        created_at=NOW - datetime.timedelta(hours=1), session_id=OLDER_SESSION
    )


async def test_the_session_cursor_reaches_the_store_and_the_last_page_offers_none() -> None:
    """Paging belongs in the query: filtering a page here would return fewer rows than asked for
    and read as the end of the corpus."""
    reader = _Reader()
    cursor = ConversationCursor.of(_conversation(OLDER_SESSION, NOW - datetime.timedelta(hours=1)))

    async with Client(build_mcp(reader)) as client:
        result = await client.call_tool("list_conversations", {"limit": 1, "cursor": cursor.model_dump(mode="json")})

    assert reader.conversation_cursors == [cursor]
    page = ConversationPage.model_validate(result.structured_content)
    assert [conversation.session_id for conversation in page.items] == [OLDER_SESSION]
    assert page.next_cursor is None


async def test_a_cursor_names_the_first_row_the_page_did_not_return() -> None:
    """Not the last row it did — so it is a position a caller can also arrive at from elsewhere,
    which is what makes a transcript entry's `first_frame_seq` a cursor as it stands."""
    reader = _Reader(*(_frame(seq) for seq in (1, 2, 3, 4)))

    async with Client(build_mcp(reader)) as client:
        result = await client.call_tool("read_rollout", {"session_id": str(SESSION), "limit": 2})

    assert [frame.frame_seq for frame in result.data.items] == [1, 2]
    assert result.data.next_cursor.frame_seq == 3


async def test_a_short_page_is_the_last_one() -> None:
    """Otherwise a reader pages forever, asking for rows that do not exist."""
    reader = _Reader(_frame(1), _frame(2))

    async with Client(build_mcp(reader)) as client:
        result = await client.call_tool("read_rollout", {"session_id": str(SESSION), "limit": 25})

    assert result.data.next_cursor is None


async def test_the_cursor_reaches_the_store_rather_than_being_filtered_here() -> None:
    """Paging has to happen in the query; filtering a page after the fact would return
    fewer rows than asked for and read as the end of the log."""
    reader = _Reader(*(_frame(seq) for seq in (1, 2, 3)))

    async with Client(build_mcp(reader)) as client:
        await client.call_tool(
            "read_rollout", {"session_id": str(SESSION), "cursor": {"frame_seq": 2}, "kinds": ["assistant"]}
        )

    # 26 rather than 25: the extra row is how the page tells "exactly full" from "more to come".
    assert reader.queries == [
        {"session_id": SESSION, "cursor": FrameCursor(frame_seq=2), "limit": 26, "kinds": ["assistant"]}
    ]


async def test_a_page_stops_on_its_byte_budget_and_says_where() -> None:
    """A row limit alone does not bound a response: one tool result can be a whole file. The
    frame that would overrun starts the next page rather than being dropped from this one."""
    reader = _Reader(_big_frame(1), _big_frame(2), _frame(3))

    async with Client(build_mcp(reader)) as client:
        result = await client.call_tool("read_rollout", {"session_id": str(SESSION), "limit": 25})

    assert [frame.frame_seq for frame in result.data.items] == [1]
    assert result.data.next_cursor.frame_seq == 2, "the overrunning frame is where the reader resumes"
    assert result.data.items[0].clipped_bytes is None, "a frame that fits is never clipped"


async def test_a_frame_larger_than_a_whole_page_is_clipped_rather_than_wedging_the_cursor() -> None:
    """Skipping it would leave the cursor unable to advance past it, and a reader looping on the
    same page forever. It goes out with its size instead, for `read_frame` to fetch."""
    reader = _Reader(_frame(1, payload={"type": "user", "content": "x" * (MAX_PAGE_BYTES * 2)}), _frame(2))

    async with Client(build_mcp(reader)) as client:
        result = await client.call_tool("read_rollout", {"session_id": str(SESSION), "limit": 25})

    [only] = result.data.items
    assert only.payload is None
    assert only.clipped_bytes > MAX_PAGE_BYTES
    assert result.data.next_cursor.frame_seq == 2


async def test_an_oversized_last_frame_ends_the_walk() -> None:
    """The clipped frame is the last one there is, so there is nothing to resume at. Naming it as
    the cursor would send the reader back for a page it has already seen."""
    reader = _Reader(_frame(1, payload={"type": "user", "content": "x" * (MAX_PAGE_BYTES * 2)}))

    async with Client(build_mcp(reader)) as client:
        result = await client.call_tool("read_rollout", {"session_id": str(SESSION), "limit": 25})

    assert result.data.items[0].clipped_bytes > MAX_PAGE_BYTES
    assert result.data.next_cursor is None


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
    """A read that started at "the first frame at or after 5" would answer a request for frame 5
    with frame 6 — the wrong frame, indistinguishable from the right one."""
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

    [turn] = result.data.items
    assert (turn.first_frame_seq, turn.last_frame_seq) == (1, 4)
    assert turn.outcome == "answered"
    assert turn.usage.cost_usd == 0.0125


async def test_a_transcript_entry_reads_as_the_conversation_rather_than_the_protocol() -> None:
    """Nothing an MCP caller sees here is `assistant`, a content block or a `tool_use_result`."""
    reader = _Reader(transcript=[_message(0, first_frame_seq=3, last_frame_seq=5)])

    async with Client(build_mcp(reader)) as client:
        result = await client.call_tool("read_transcript", {"session_id": str(SESSION)})

    entry = one(_transcript(result).items)
    assert isinstance(entry, MessageEntry)
    assert entry.text == "answer 0"
    assert entry.message.opened_at_frame_seq == 3


async def test_an_entrys_provenance_is_a_frame_cursor_with_no_arithmetic() -> None:
    """The reason provenance exists: appeal a normalization to the frames behind it. An
    exclusive cursor would need a `- 1` here, and an off-by-one reads the wrong frame while
    looking right."""
    reader = _Reader(_frame(3), _frame(4), transcript=[_message(0, first_frame_seq=3, last_frame_seq=4)])

    async with Client(build_mcp(reader)) as client:
        entry = one(_transcript(await client.call_tool("read_transcript", {"session_id": str(SESSION)})).items)
        assert isinstance(entry.provenance, FromFrames)
        named = await client.call_tool(
            "read_frame", {"session_id": str(SESSION), "frame_seq": entry.provenance.first_frame_seq}
        )
        span = await client.call_tool(
            "read_rollout", {"session_id": str(SESSION), "cursor": {"frame_seq": entry.provenance.first_frame_seq}}
        )

    assert named.data.frame_seq == 3
    assert [frame.frame_seq for frame in span.data.items] == [3, 4]


async def test_a_transcript_cursor_resumes_where_the_page_stopped() -> None:
    reader = _Reader(transcript=[_message(index, first_frame_seq=index + 1) for index in range(4)])

    async with Client(build_mcp(reader)) as client:
        first = await client.call_tool("read_transcript", {"session_id": str(SESSION), "limit": 2})
        second = await client.call_tool(
            "read_transcript", {"session_id": str(SESSION), "limit": 2, "cursor": {"index": 2}}
        )

    assert [entry.index for entry in _transcript(first).items] == [0, 1]
    assert _transcript(first).next_cursor == TranscriptCursor(index=2)
    assert [entry.index for entry in _transcript(second).items] == [2, 3]
    assert _transcript(second).next_cursor is None


async def test_an_oversized_tool_result_loses_its_structured_half_not_its_provenance() -> None:
    """`structured` is the part that is routinely a whole file. Dropping it keeps the entry — and
    the frames it came from — readable, which a page that simply refused it would not."""
    reader = _Reader(transcript=[_tool_result(0, structured={"stdout": "x" * (MAX_PAGE_BYTES * 2)})])

    async with Client(build_mcp(reader)) as client:
        result = await client.call_tool("read_transcript", {"session_id": str(SESSION)})

    entry = one(_transcript(result).items)
    assert isinstance(entry, ToolResultEntry)
    assert entry.structured is None
    assert entry.clipped_bytes is not None
    assert entry.clipped_bytes > MAX_PAGE_BYTES
    assert entry.provenance == FromFrames(first_frame_seq=1, last_frame_seq=1)


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
