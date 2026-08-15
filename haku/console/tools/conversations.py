"""haku-console's in-process `haku_conversations` MCP server — reading past sessions.

Lets Haku consult what it actually did in an earlier session, rather than starting each one
from the last twenty room messages and nothing else (`haku/plans/matrix_chat_runtime.md` R5.4a,
Phase 5). The corpus is `claude_chat_frames`, the console's verbatim record of the agent
protocol, so a tool call and the result it got are both there — which no other table has.

**A drilldown, not a dump.** `list_conversations` finds the session, `read_rollout` pages its
frames, and a `kinds` filter is how you skim before reading. Context is the scarce resource, so
both the page size and each frame's payload are capped, and a clipped frame says so rather than
silently returning less than the record holds.

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
from typing import Annotated, Any, Literal, Protocol

from fastmcp import FastMCP
from pydantic import BaseModel, Field

HAKU_CONVERSATIONS_SERVER_ID = "haku_conversations"

# Rows per page. Small on purpose: a frame carries a whole tool result, and the console's
# past-tool-calls page already learned that asking for hundreds of such rows means a
# multi-megabyte response (haku/console/debug/past_tool_calls_perf.md).
MAX_PAGE = 100
DEFAULT_PAGE = 25

# Bytes of one frame's JSON before it is clipped. A row limit alone does not bound a response,
# because one `tool_result` can be an entire file.
MAX_FRAME_BYTES = 8_000


class Conversation(BaseModel):
    session_id: str
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

    turn_id: str
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
        self, session_id: str, *, after_seq: int | None, limit: int, kinds: Sequence[str] | None
    ) -> list[RolloutFrame]: ...

    async def list_turns(self, session_id: str, *, limit: int) -> list[TurnRecord]: ...


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
        "its protocol frames. The rollout holds tool calls together with their results, which the "
        "room transcript does not. Read-only.",
    )

    @mcp.tool
    async def list_conversations(
        limit: Annotated[int, Field(default=20, ge=1, le=MAX_PAGE, description="Most recent sessions first.")] = 20,
    ) -> list[Conversation]:
        """List Haku's past chat sessions, newest first."""
        return await reader.list_conversations(limit=limit)

    @mcp.tool
    async def read_rollout(
        session_id: Annotated[str, Field(description="From `list_conversations`.")],
        after_seq: Annotated[
            int | None,
            Field(default=None, description="Read frames after this `frame_seq`; omit to start at the beginning."),
        ] = None,
        limit: Annotated[int, Field(default=DEFAULT_PAGE, ge=1, le=MAX_PAGE)] = DEFAULT_PAGE,
        kinds: Annotated[
            list[Literal["assistant", "user", "result", "system", "command_lifecycle", "stream_event"]] | None,
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
    async def list_turns(
        session_id: Annotated[str, Field(description="From `list_conversations`.")],
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
