"""haku-console's in-process `haku_conversations` MCP server — reading past sessions.

Lets Haku consult what it actually did in an earlier session, rather than starting each one
from the last twenty room messages and nothing else (`haku/plans/matrix_chat_runtime.md` R5.4a,
Phase 5). The corpus is `claude_chat_frames`, the console's verbatim record of the agent
protocol, so a tool call and the result it got are both there — which no other table has.

**A drilldown, not a dump.** `list_conversations` finds the session, `read_rollout` pages its
frames, and a `kinds` filter is how you skim before reading. Context is the scarce resource, so
both the page size and each frame's payload are capped, and a clipped frame says so rather than
silently returning less than the record holds.

**Shaped as a cursor over the log, not as turns.** `frame_seq` already totally orders a
session, and a turn is the console's interpretation rather than the record: the CLI folds a
mid-turn prompt into the running turn, so one `result` can answer two prompts and a
turn-shaped read would have to pick which prompt an exchange belonged to. Turn brackets are a
separate, runtime-motivated concern (`haku/plans/chat_runtime_cleanup.md` §1).

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
            list[Literal["assistant", "user", "result", "system", "command_lifecycle"]] | None,
            Field(
                default=None,
                description="Only these frame types. `assistant` and `user` together are the conversation "
                "with its tool calls and results; omit to read everything.",
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

    return mcp
