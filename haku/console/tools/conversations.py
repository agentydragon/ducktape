"""haku-console's in-process `haku_conversations` MCP server — reading past sessions.

Lets Haku consult what it actually did in an earlier session, rather than starting each one
from the last twenty room messages and nothing else (`haku/plans/matrix_chat_runtime.md` R5.4a,
Phase 5). The corpus is `session_frames`, the console's verbatim record of the agent
protocol, so a tool call and the result it got are both there — which no other table has.

**A drilldown, not a dump.** `list_conversations` finds the session, `read_rollout` pages its
frames, and a `kinds` filter is how you skim before reading. Context is the scarce resource, so
both the page size and each frame's payload are capped, and a clipped frame says so rather than
silently returning less than the record holds. `read_frame` is the bottom of the drilldown: one
frame, whole, however large — because a cap that cannot be escaped is a record that cannot be
read. The census of production frames was blind to every `control_response` in the corpus for
exactly that reason.

**Reading is a cursor over the log; turns are an index into it.** `frame_seq` already totally
orders a session, so `read_rollout` needs no notion of a turn — and a turn is the console's
interpretation rather than the record, since the CLI folds a mid-turn prompt into the running
turn and one `result` can then answer two prompts. `list_turns` therefore reports each exchange
as a *range* over the same log, with what it cost and how it ended: enough to pick the exchange
worth reading, without the frames themselves being reshaped around it.

**Reads are unscoped** (R5.3a): any session, whichever room it served. Deliberate for now —
the eventual policy about which Haku may read which past conversation is not settled, and
guessing at one here would be a scoping rule nobody stated.
"""

from __future__ import annotations

import datetime
import json
from collections.abc import Sequence
from typing import Annotated, Any, Literal, Protocol, get_args
from uuid import UUID

from fastmcp import FastMCP
from pydantic import BaseModel, Field

HAKU_CONVERSATIONS_SERVER_ID = "haku_conversations"

# Rows per page. Small on purpose: a frame carries a whole tool result, and the console's
# past-tool-calls page already learned that asking for hundreds of such rows means a
# multi-megabyte response (haku/console/debug/past_tool_calls_perf.md).
MAX_PAGE = 100
DEFAULT_PAGE = 25

# Bytes of one frame's JSON before it is clipped by `read_rollout`. A row limit alone does not
# bound a response, because one `tool_result` can be an entire file. `read_frame` does not apply
# it: one frame is already a bounded response.
MAX_FRAME_BYTES = 8_000

# What a session's `kind` column actually holds, so a caller can filter for any of it. Every
# entry was observed in production; four of them are absent from the CLI's `protocol.md`, and
# `setup_output` is the console's own (`haku/console/x/session_frames.py`). Spelled here rather
# than imported because this server is in the stable catalog and the vocabulary lives in the
# experimental chat runtime — the same reason `RolloutReader` is a port.
FrameKind = Literal[
    "assistant",
    "user",
    "result",
    "system",
    "command_lifecycle",
    "control_request",
    "control_response",
    "rate_limit_event",
    "setup_output",
    "stream_event",
    "tool_progress",
]


class Conversation(BaseModel):
    session_id: UUID
    surface: str | None = Field(description="`matrix` or `spa`; absent on sessions that predate the column.")
    room_id: str | None = Field(description="The Matrix room this session served, if it served one.")
    status: str
    created_at: datetime.datetime
    error: str | None = None


class RolloutFrame(BaseModel):
    frame_seq: int = Field(description="Pass the last one back as `after_seq` to read the next page.")
    direction: str = Field(description="`to_agent` for what the console sent, `from_agent` for what came back.")
    kind: str = Field(description="The frame's protocol `type`: assistant, user, result, system, …")
    created_at: datetime.datetime
    payload: dict[str, Any] | None = Field(
        description="The frame exactly as it crossed the wire, or absent when it was clipped for size."
    )
    clipped_bytes: int | None = Field(
        default=None, description="Set instead of `payload` when the frame was too large to return; its size in bytes."
    )
    partial: bool = Field(
        description="True for the console's reconstruction of an answer that was still streaming — "
        "so a `partial` frame at the end of a session is a turn that never finished."
    )


class RolloutPage(BaseModel):
    frames: list[RolloutFrame]
    next_after_seq: int | None = Field(
        description="Cursor for the following page, or absent when this page is the last."
    )


class TurnRecord(BaseModel):
    """One exchange of a session, as a range over that session's frames."""

    turn_id: UUID
    first_frame_seq: int = Field(description="Pass as `after_seq` minus one to `read_rollout` to read this exchange.")
    last_frame_seq: int | None = Field(
        description="Inclusive end of the range. Absent while the exchange is still running, "
        "and on a finished one that recorded no frames at all."
    )
    started_at: datetime.datetime
    ended_at: datetime.datetime | None = Field(description="Absent while the exchange is still running.")
    outcome: str | None = Field(description="`answered`, `aborted` or `failed`; absent while it is still running.")
    cost_usd: float | None = None
    duration_ms: int | None = None
    usage: dict[str, Any] | None = Field(default=None, description="The model's own token accounting for the exchange.")


class RolloutReader(Protocol):
    """The console's rollout store, as this server needs it.

    A port rather than an import: the store lives in the experimental chat runtime, and a
    server registered in the stable catalog should not depend on that package's shape.
    """

    async def list_conversations(self, *, limit: int) -> list[Conversation]: ...

    # `Sequence` rather than `list` so the tool can narrow `kinds` to a Literal union for its
    # generated schema: `list` is invariant, so `list[Literal[...]]` would not satisfy `list[str]`.
    async def read_frames(
        self, session_id: UUID, *, after_seq: int | None, limit: int, kinds: Sequence[str] | None
    ) -> list[RolloutFrame]: ...

    async def list_turns(self, session_id: UUID, *, limit: int) -> list[TurnRecord]: ...


def clip(frame: RolloutFrame) -> RolloutFrame:
    """Drop an oversized payload, recording what was there.

    Clipping rather than truncating the JSON: half an object is not parseable and reads as
    corruption, where a stated size and a missing payload is a fact the caller can act on.
    """
    if frame.payload is None:
        return frame
    size = len(json.dumps(frame.payload))
    if size <= MAX_FRAME_BYTES:
        return frame
    return frame.model_copy(update={"payload": None, "clipped_bytes": size})


def build_mcp(reader: RolloutReader) -> FastMCP:
    mcp: FastMCP = FastMCP(
        name=HAKU_CONVERSATIONS_SERVER_ID,
        instructions="Read Haku's own past sessions: `list_conversations` to find one, `read_rollout` to page "
        "its protocol frames, `read_frame` for one of them in full when the page clipped it. The "
        "rollout holds tool calls together with their results, which the room transcript does not. "
        "Read-only.",
    )

    @mcp.tool
    async def list_conversations(
        limit: Annotated[int, Field(default=20, ge=1, le=MAX_PAGE, description="Most recent sessions first.")] = 20,
    ) -> list[Conversation]:
        """List Haku's past chat sessions, newest first."""
        return await reader.list_conversations(limit=limit)

    @mcp.tool
    async def read_rollout(
        session_id: Annotated[UUID, Field(description="From `list_conversations`.")],
        after_seq: Annotated[
            int | None,
            Field(default=None, description="Read frames after this `frame_seq`; omit to start at the beginning."),
        ] = None,
        limit: Annotated[int, Field(default=DEFAULT_PAGE, ge=1, le=MAX_PAGE)] = DEFAULT_PAGE,
        kinds: Annotated[
            list[FrameKind] | None,
            Field(
                default=None,
                description="Only these frame types. `assistant` and `user` together are the conversation "
                "with its tool calls and results; omit for everything except `stream_event`, which is "
                "one token batch of an answer that arrives whole a moment later and so has to be asked "
                "for by name — worth doing only to see how far an answer got before it was cut off.",
            ),
        ] = None,
    ) -> RolloutPage:
        """Read one session's protocol frames in order, a page at a time."""
        frames = [
            clip(frame) for frame in await reader.read_frames(session_id, after_seq=after_seq, limit=limit, kinds=kinds)
        ]
        # A short page is the last one. Cheaper than a second count query, and the caller only
        # needs to know whether to ask again.
        return RolloutPage(frames=frames, next_after_seq=frames[-1].frame_seq if len(frames) == limit else None)

    @mcp.tool
    async def read_frame(
        session_id: Annotated[UUID, Field(description="From `list_conversations`.")],
        frame_seq: Annotated[int, Field(description="The `frame_seq` of the frame to read, from `read_rollout`.")],
    ) -> RolloutFrame:
        """One frame in full, however large — the way to read what `read_rollout` clipped.

        A page clips an oversized payload because a page of them is unbounded; naming one frame
        bounds the response by that frame alone. This is the only route to the frames a page can
        never show: a `control_response`, a `system` init, a tool result of tens of kilobytes.

        Deltas (`stream_event`) are readable here too, but never need to be — one is a few
        characters of an answer, and nothing clips them.
        """
        # `after_seq` is exclusive, so the frame asked for is the first row after its predecessor.
        # `kinds=None` would drop deltas from the query, and a caller naming a `frame_seq` has
        # already chosen its row.
        frames = await reader.read_frames(session_id, after_seq=frame_seq - 1, limit=1, kinds=list(get_args(FrameKind)))
        if not frames or frames[0].frame_seq != frame_seq:
            raise ValueError(f"no such frame: {session_id=} {frame_seq=}")
        return frames[0]

    @mcp.tool
    async def list_turns(
        session_id: Annotated[UUID, Field(description="From `list_conversations`.")],
        limit: Annotated[int, Field(default=DEFAULT_PAGE, ge=1, le=MAX_PAGE, description="Newest exchange first.")] = (
            DEFAULT_PAGE
        ),
    ) -> list[TurnRecord]:
        """List a session's exchanges — what it cost, how long it took, how each one ended.

        Each carries the frame range it produced, so this is the cheap way to find the exchange
        worth reading before paging its frames.
        """
        return await reader.list_turns(session_id, limit=limit)

    return mcp
