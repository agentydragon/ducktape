"""Tests for the in-process `haku_conversations` MCP server (build_mcp)."""

from __future__ import annotations

import datetime
from collections.abc import Sequence
from uuid import UUID

import pytest
import pytest_bazel
from fastmcp import Client
from fastmcp.client.client import CallToolResult
from more_itertools import one

from haku.console.chat_models import BridgeFrameKind, RuntimeKind
from haku.console.in_process_server_access import InProcessServerAccessPolicy
from haku.console.mcp_config import AccessProfile
from haku.console.mcp_execution import (
    AgentMcpExecutionCaller,
    McpExecutionContext,
    OperatorMcpExecutionCaller,
    mcp_execution_request_meta,
)
from haku.console.tool_call_actor import AgentActor, OperatorActor, ToolCallActor
from haku.console.tools.conversations import (
    HAKU_CONVERSATIONS_SERVER_ID,
    MAX_PAGE_BYTES,
    SessionPage,
    TranscriptPage,
    build_mcp,
)
from haku.console.x.conversation_records import (
    ChannelAttachment,
    FrameCursor,
    FromFrames,
    MessageEntry,
    Outcome,
    RolloutFrame,
    SessionCursor,
    SessionRecord,
    ToolResultEntry,
    TranscriptCursor,
    TranscriptEntry,
    TranscriptSlice,
    TurnAnsweredEnd,
    TurnCursor,
    TurnRecord,
)

SESSION = UUID("11111111-1111-1111-1111-111111111111")
CONVERSATION = UUID("44444444-4444-4444-4444-444444444444")
OLDER_SESSION = UUID("33333333-3333-3333-3333-333333333333")
TURN = UUID("22222222-2222-2222-2222-222222222222")
NOW = datetime.datetime(2026, 8, 12, 9, 0, tzinfo=datetime.UTC)
HAKU = AgentActor(
    agent_id=UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
    operator_id=UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"),
    binding_id=UUID("cccccccc-cccc-cccc-cccc-cccccccccccc"),
    access_profile_id="haku",
)
CODER = AgentActor(
    agent_id=UUID("dddddddd-dddd-dddd-dddd-dddddddddddd"),
    operator_id=HAKU.operator_id,
    binding_id=UUID("eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"),
    access_profile_id="public-coder",
)
ACCESS = InProcessServerAccessPolicy(
    (AccessProfile(id="haku", auto_approval_policy="manual", in_process_server_ids={"haku_conversations"}),)
)

# Every tool that pages, and how a page of it is asked for. The point of the surface is that this
# list can be walked with one loop, so the tests below walk it.
PAGED_TOOLS: tuple[tuple[str, dict[str, str]], ...] = (
    ("list_sessions", {}),
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


def _session(session_id: UUID, created_at: datetime.datetime) -> SessionRecord:
    return SessionRecord(
        session_id=session_id,
        conversation_id=CONVERSATION,
        runtime_kind=RuntimeKind.CLAUDE_CODE,
        attachments=[ChannelAttachment(surface="matrix", address="!room:example.org", attached_at=created_at)],
        status="closed",
        created_at=created_at,
    )


def _frame(seq: int, kind: str = "assistant", payload: dict | None = None) -> RolloutFrame:
    return RolloutFrame(
        frame_seq=seq,
        direction="from_agent",
        kind=BridgeFrameKind.HARNESS_FRAME,
        created_at=NOW,
        payload=payload if payload is not None else {"type": kind},
    )


def _message(index: int, *, first_frame_seq: int, last_frame_seq: int | None = None) -> MessageEntry:
    return MessageEntry(
        index=index,
        provenance=FromFrames(first_frame_seq=first_frame_seq, last_frame_seq=last_frame_seq or first_frame_seq),
        text=f"answer {index}",
        backend_item_id=f"msg_{index}",
    )


def _tool_result(index: int, *, structured: object) -> ToolResultEntry:
    return ToolResultEntry(
        index=index,
        provenance=FromFrames(first_frame_seq=index + 1, last_frame_seq=index + 1),
        call_id=f"toolu_{index}",
        content="ok",
        structured=structured,
        outcome=Outcome.UNKNOWN,
    )


class _Reader:
    """A `ConversationReader` over lists, recording how it was queried."""

    def __init__(self, *frames: RolloutFrame, transcript: Sequence[TranscriptEntry] = ()):
        self._frames = list(frames)
        self._transcript = list(transcript)
        self.queries: list[dict] = []
        self.session_cursors: list[SessionCursor | None] = []
        # Newest first, the order the store lists them in.
        self._sessions = [_session(SESSION, NOW), _session(OLDER_SESSION, NOW - datetime.timedelta(hours=1))]

    async def list_sessions(self, *, cursor: SessionCursor | None, limit: int) -> list[SessionRecord]:
        self.session_cursors.append(cursor)
        return [
            session
            for session in self._sessions
            if cursor is None or (session.created_at, session.session_id) <= (cursor.created_at, cursor.session_id)
        ][:limit]

    async def read_frames(
        self,
        session_id: UUID,
        *,
        cursor: FrameCursor | None,
        limit: int,
        kinds: Sequence[BridgeFrameKind] | None = None,
    ) -> list[RolloutFrame]:
        self.queries.append({"session_id": session_id, "cursor": cursor, "limit": limit, "kinds": kinds})
        selected = [frame for frame in self._frames if kinds is None or frame.kind in kinds]
        if cursor is not None:
            selected = [frame for frame in selected if frame.frame_seq >= cursor.frame_seq]
        return selected[:limit]

    async def read_frame(self, session_id: UUID, frame_seq: int) -> RolloutFrame | None:
        self.queries.append({"session_id": session_id, "frame_seq": frame_seq})
        return next((frame for frame in self._frames if frame.frame_seq == frame_seq), None)

    async def list_turns(self, session_id: UUID, *, cursor: TurnCursor | None, limit: int) -> list[TurnRecord]:
        self.queries.append({"session_id": session_id, "cursor": cursor, "limit": limit})
        return [
            TurnRecord(
                turn_id=TURN, first_frame_seq=1, last_frame_seq=4, started_at=NOW, ended_at=NOW, end=TurnAnsweredEnd()
            )
        ][:limit]

    async def read_transcript(
        self, session_id: UUID, *, cursor: TranscriptCursor | None, limit: int
    ) -> TranscriptSlice:
        self.queries.append({"session_id": session_id, "cursor": cursor, "limit": limit})
        start = cursor.index if cursor is not None else 0
        return TranscriptSlice(entries=self._transcript[start : start + limit], unreadable=None)


def _mcp(reader: _Reader):
    return build_mcp(reader, access=ACCESS)


def _meta(actor: ToolCallActor = HAKU) -> dict[str, object]:
    caller = (
        AgentMcpExecutionCaller(agent_id=actor.agent_id, access_profile_id=actor.access_profile_id)
        if isinstance(actor, AgentActor)
        else OperatorMcpExecutionCaller(operator_id=actor.operator_id)
    )
    return mcp_execution_request_meta(McpExecutionContext(caller=caller, tool_call_id="tc_test"))


async def _call(client: Client, tool: str, arguments: dict, *, actor: ToolCallActor = HAKU, **kwargs):
    return await client.call_tool(tool, arguments, meta=_meta(actor), **kwargs)


async def test_tool_surface() -> None:
    async with Client(_mcp(_Reader())) as client:
        tools = {tool.name for tool in await client.list_tools()}

    assert tools == {"list_sessions", "list_turns", "read_transcript", "read_rollout", "read_frame"}
    assert HAKU_CONVERSATIONS_SERVER_ID == "haku_conversations"


async def test_ungranted_actor_cannot_read_conversations() -> None:
    reader = _Reader()
    async with Client(_mcp(reader)) as client:
        result = await _call(client, "list_sessions", {}, actor=CODER, raise_on_error=False)
    assert result.is_error
    assert reader.session_cursors == []


@pytest.mark.parametrize(
    "actor",
    [
        AgentActor(agent_id=UUID(int=1), operator_id=HAKU.operator_id, binding_id=UUID(int=2)),
        AgentActor(
            agent_id=UUID(int=3), operator_id=HAKU.operator_id, binding_id=UUID(int=4), access_profile_id="missing"
        ),
        OperatorActor(operator_id=HAKU.operator_id),
    ],
)
async def test_unprofiled_unknown_and_operator_actors_cannot_read_conversations(actor: ToolCallActor) -> None:
    reader = _Reader()
    async with Client(_mcp(reader)) as client:
        result = await _call(client, "list_sessions", {}, actor=actor, raise_on_error=False)
    assert result.is_error
    assert reader.session_cursors == []


async def test_every_listing_answers_in_the_same_shape() -> None:
    """The point of the surface: one loop reads any of them. A tool that grew its own envelope
    would pass its own test and still break that."""
    reader = _Reader(_frame(1), transcript=[_message(0, first_frame_seq=1)])

    async with Client(_mcp(reader)) as client:
        for tool, arguments in PAGED_TOOLS:
            result = await _call(client, tool, arguments)
            assert result.data.items, tool
            assert hasattr(result.data, "next_cursor"), tool


async def test_a_session_names_its_thread_and_the_channels_holding_a_copy_of_it() -> None:
    """The thread rather than a surface enum: what a reader groups sessions by, and what tells it
    where the same conversation is also being read."""
    async with Client(_mcp(_Reader())) as client:
        result = await _call(client, "list_sessions", {})

    assert not result.is_error
    page = SessionPage.model_validate(result.structured_content)
    assert page.items[0].conversation_id == CONVERSATION
    assert page.items[0].runtime_kind == "claude_code"
    assert [attachment.address for attachment in page.items[0].attachments] == ["!room:example.org"]


async def test_a_full_page_of_sessions_names_both_halves_of_the_key_in_its_cursor() -> None:
    """`created_at` alone does not order the corpus — two sessions can start in one instant — so
    the cursor has to carry the tiebreak rather than pretend one column suffices."""
    async with Client(_mcp(_Reader())) as client:
        result = await _call(client, "list_sessions", {"limit": 1})

    page = SessionPage.model_validate(result.structured_content)
    assert [session.session_id for session in page.items] == [SESSION]
    assert page.next_cursor == SessionCursor(created_at=NOW - datetime.timedelta(hours=1), session_id=OLDER_SESSION)


async def test_the_session_cursor_reaches_the_store_and_the_last_page_offers_none() -> None:
    """Paging belongs in the query: filtering a page here would return fewer rows than asked for
    and read as the end of the corpus."""
    reader = _Reader()
    cursor = SessionCursor.of(_session(OLDER_SESSION, NOW - datetime.timedelta(hours=1)))

    async with Client(_mcp(reader)) as client:
        result = await _call(client, "list_sessions", {"limit": 1, "cursor": cursor.model_dump(mode="json")})

    assert reader.session_cursors == [cursor]
    page = SessionPage.model_validate(result.structured_content)
    assert [session.session_id for session in page.items] == [OLDER_SESSION]
    assert page.next_cursor is None


async def test_a_cursor_names_the_first_row_the_page_did_not_return() -> None:
    """Not the last row it did — so it is a position a caller can also arrive at from elsewhere,
    which is what makes a transcript entry's `first_frame_seq` a cursor as it stands."""
    reader = _Reader(*(_frame(seq) for seq in (1, 2, 3, 4)))

    async with Client(_mcp(reader)) as client:
        result = await _call(client, "read_rollout", {"session_id": str(SESSION), "limit": 2})

    assert [frame.frame_seq for frame in result.data.items] == [1, 2]
    assert result.data.next_cursor.frame_seq == 3


async def test_a_short_page_is_the_last_one() -> None:
    """Otherwise a reader pages forever, asking for rows that do not exist."""
    reader = _Reader(_frame(1), _frame(2))

    async with Client(_mcp(reader)) as client:
        result = await _call(client, "read_rollout", {"session_id": str(SESSION), "limit": 25})

    assert result.data.next_cursor is None


async def test_rollout_returns_discriminator_free_native_json_unchanged() -> None:
    native = {"阶段": "最终", "正文": "你好", "成功": True}
    reader = _Reader(_frame(1, payload=native))

    async with Client(_mcp(reader)) as client:
        result = await _call(client, "read_rollout", {"session_id": str(SESSION)})

    assert result.data.items[0].payload == native


async def test_the_cursor_reaches_the_store_rather_than_being_filtered_here() -> None:
    """Paging has to happen in the query; filtering a page after the fact would return
    fewer rows than asked for and read as the end of the log."""
    reader = _Reader(*(_frame(seq) for seq in (1, 2, 3)))

    async with Client(_mcp(reader)) as client:
        await _call(
            client, "read_rollout", {"session_id": str(SESSION), "cursor": {"frame_seq": 2}, "kinds": ["harness_frame"]}
        )

    # 26 rather than 25: the extra row is how the page tells "exactly full" from "more to come".
    assert reader.queries == [
        {
            "session_id": SESSION,
            "cursor": FrameCursor(frame_seq=2),
            "limit": 26,
            "kinds": [BridgeFrameKind.HARNESS_FRAME],
        }
    ]


async def test_a_page_stops_on_its_byte_budget_and_says_where() -> None:
    """A row limit alone does not bound a response: one tool result can be a whole file. The
    frame that would overrun starts the next page rather than being dropped from this one."""
    reader = _Reader(_big_frame(1), _big_frame(2), _frame(3))

    async with Client(_mcp(reader)) as client:
        result = await _call(client, "read_rollout", {"session_id": str(SESSION), "limit": 25})

    assert [frame.frame_seq for frame in result.data.items] == [1]
    assert result.data.next_cursor.frame_seq == 2, "the overrunning frame is where the reader resumes"
    assert result.data.items[0].clipped_bytes is None, "a frame that fits is never clipped"


async def test_a_frame_larger_than_a_whole_page_is_clipped_rather_than_wedging_the_cursor() -> None:
    """Skipping it would leave the cursor unable to advance past it, and a reader looping on the
    same page forever. It goes out with its size instead, for `read_frame` to fetch."""
    reader = _Reader(_frame(1, payload={"type": "user", "content": "x" * (MAX_PAGE_BYTES * 2)}), _frame(2))

    async with Client(_mcp(reader)) as client:
        result = await _call(client, "read_rollout", {"session_id": str(SESSION), "limit": 25})

    [only] = result.data.items
    assert only.payload is None
    assert only.clipped_bytes > MAX_PAGE_BYTES
    assert result.data.next_cursor.frame_seq == 2


async def test_an_oversized_last_frame_ends_the_walk() -> None:
    """The clipped frame is the last one there is, so there is nothing to resume at. Naming it as
    the cursor would send the reader back for a page it has already seen."""
    reader = _Reader(_frame(1, payload={"type": "user", "content": "x" * (MAX_PAGE_BYTES * 2)}))

    async with Client(_mcp(reader)) as client:
        result = await _call(client, "read_rollout", {"session_id": str(SESSION), "limit": 25})

    assert result.data.items[0].clipped_bytes > MAX_PAGE_BYTES
    assert result.data.next_cursor is None


async def test_one_named_frame_comes_back_whole_however_large() -> None:
    """The escape hatch: a page has a budget to spend, and a single named frame is the response."""
    big = _frame(1, payload={"type": "user", "content": "x" * (MAX_PAGE_BYTES * 2)})
    reader = _Reader(big, _frame(2))

    async with Client(_mcp(reader)) as client:
        result = await _call(client, "read_frame", {"session_id": str(SESSION), "frame_seq": 1})

    assert result.data.payload == big.payload
    assert result.data.clipped_bytes is None


async def test_a_named_frame_is_read_whole_without_native_classification() -> None:
    reader = _Reader(_frame(7, kind="stream_event"))

    async with Client(_mcp(reader)) as client:
        result = await _call(client, "read_frame", {"session_id": str(SESSION), "frame_seq": 7})

    assert result.data.kind == BridgeFrameKind.HARNESS_FRAME
    assert result.data.payload == {"type": "stream_event"}
    assert reader.queries == [{"session_id": SESSION, "frame_seq": 7}]


async def test_a_named_method_only_frame_is_returned_as_opaque_json() -> None:
    frame = _frame(
        8,
        kind="codex/event/unknown",
        payload={"jsonrpc": "2.0", "method": "codex/event/unknown", "params": {"opaque": True}},
    )
    reader = _Reader(frame)

    async with Client(_mcp(reader)) as client:
        result = await _call(client, "read_frame", {"session_id": str(SESSION), "frame_seq": 8})

    assert result.data.payload == frame.payload


async def test_a_frame_seq_that_does_not_exist_is_an_error_not_the_next_frame() -> None:
    """A read that started at "the first frame at or after 5" would answer a request for frame 5
    with frame 6 — the wrong frame, indistinguishable from the right one."""
    reader = _Reader(_frame(4), _frame(6))

    async with Client(_mcp(reader)) as client:
        result = await _call(client, "read_frame", {"session_id": str(SESSION), "frame_seq": 5}, raise_on_error=False)

    assert result.is_error


async def test_a_turn_carries_the_range_to_read() -> None:
    """The point of listing exchanges is to pick one and then read its frames, so the bracket is
    what a listing has to come back with."""
    async with Client(_mcp(_Reader())) as client:
        result = await _call(client, "list_turns", {"session_id": str(SESSION)})

    [turn] = result.data.items
    assert (turn.first_frame_seq, turn.last_frame_seq) == (1, 4)
    assert turn.end == {"outcome": "answered"}


async def test_a_transcript_entry_reads_as_the_conversation_rather_than_the_protocol() -> None:
    """Nothing an MCP caller sees here is `assistant`, a content block or a `tool_use_result`."""
    reader = _Reader(transcript=[_message(0, first_frame_seq=3, last_frame_seq=5)])

    async with Client(_mcp(reader)) as client:
        result = await _call(client, "read_transcript", {"session_id": str(SESSION)})

    entry = one(_transcript(result).items)
    assert isinstance(entry, MessageEntry)
    assert entry.text == "answer 0"
    assert entry.provenance == FromFrames(first_frame_seq=3, last_frame_seq=5)


async def test_an_entrys_provenance_is_a_frame_cursor_with_no_arithmetic() -> None:
    """The reason provenance exists: appeal a normalization to the frames behind it. An
    exclusive cursor would need a `- 1` here, and an off-by-one reads the wrong frame while
    looking right."""
    reader = _Reader(_frame(3), _frame(4), transcript=[_message(0, first_frame_seq=3, last_frame_seq=4)])

    async with Client(_mcp(reader)) as client:
        entry = one(_transcript(await _call(client, "read_transcript", {"session_id": str(SESSION)})).items)
        assert isinstance(entry.provenance, FromFrames)
        named = await _call(
            client, "read_frame", {"session_id": str(SESSION), "frame_seq": entry.provenance.first_frame_seq}
        )
        span = await _call(
            client,
            "read_rollout",
            {"session_id": str(SESSION), "cursor": {"frame_seq": entry.provenance.first_frame_seq}},
        )

    assert named.data.frame_seq == 3
    assert [frame.frame_seq for frame in span.data.items] == [3, 4]


async def test_a_transcript_cursor_resumes_where_the_page_stopped() -> None:
    reader = _Reader(transcript=[_message(index, first_frame_seq=index + 1) for index in range(4)])

    async with Client(_mcp(reader)) as client:
        first = await _call(client, "read_transcript", {"session_id": str(SESSION), "limit": 2})
        second = await _call(
            client, "read_transcript", {"session_id": str(SESSION), "limit": 2, "cursor": {"index": 2}}
        )

    assert [entry.index for entry in _transcript(first).items] == [0, 1]
    assert _transcript(first).next_cursor == TranscriptCursor(index=2)
    assert [entry.index for entry in _transcript(second).items] == [2, 3]
    assert _transcript(second).next_cursor is None


async def test_an_oversized_tool_result_loses_its_structured_half_not_its_provenance() -> None:
    """`structured` is the part that is routinely a whole file. Dropping it keeps the entry — and
    the frames it came from — readable, which a page that simply refused it would not."""
    reader = _Reader(transcript=[_tool_result(0, structured={"stdout": "x" * (MAX_PAGE_BYTES * 2)})])

    async with Client(_mcp(reader)) as client:
        result = await _call(client, "read_transcript", {"session_id": str(SESSION)})

    entry = one(_transcript(result).items)
    assert isinstance(entry, ToolResultEntry)
    assert entry.structured is None
    assert entry.clipped_bytes is not None
    assert entry.clipped_bytes > MAX_PAGE_BYTES
    assert entry.provenance == FromFrames(first_frame_seq=1, last_frame_seq=1)


async def test_a_page_size_above_the_cap_is_refused() -> None:
    """The cap is the only thing keeping a read from being a dump."""
    async with Client(_mcp(_Reader())) as client:
        result = await _call(
            client, "read_rollout", {"session_id": str(SESSION), "limit": 10_000}, raise_on_error=False
        )

    assert result.is_error


async def test_a_session_id_that_is_not_an_id_is_refused_here() -> None:
    """The parameter is a `UUID`, so the schema refuses this before any code runs and the store
    is never handed something it would have to validate."""
    reader = _Reader()
    async with Client(_mcp(reader)) as client:
        result = await _call(client, "read_rollout", {"session_id": "not-an-id"}, raise_on_error=False)

    assert result.is_error
    assert reader.queries == []


if __name__ == "__main__":
    pytest_bazel.main()
