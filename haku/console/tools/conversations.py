"""haku-console's in-process `haku_conversations` MCP server — reading past sessions.

Lets Haku consult what it actually did in an earlier session, rather than starting each one
from the last twenty room messages and nothing else: the room is not the only corpus and it is the
smaller one (<../x/channels/matrix/SPEC.md> § The agent's own view). What it reads are the console's
two durable records of a session: `conversation_event`, the neutral log written as each frame
arrived, and `session_frames`, the verbatim wire it was read off. A tool call and the result it got
are in both — which the room is not.

**Two readings, and the drilldown runs between them.** `read_transcript` is what a conversation
*meant* — prompts, messages, reasoning, tool calls and their results, as one vocabulary that says
nothing about which agent backend produced them. It is folded from the log rather than re-read from
the frames, so it says what was recorded at the time rather than what today's adapter would make of
them now. `read_rollout` and `read_frame` are the frames a **named** backend actually sent,
verbatim — Claude Code's today, since that is the one adapter there is, and a reader must be told
which rather than left to read them as the conversation. The first is what a reader almost always
wants; the second is the appeal, and every transcript entry carries the frame range to appeal to
(`provenance`). That path is the whole reason the transcript records where it came from, so it is
built to be walked: a `frames` provenance hands `first_frame_seq` straight to `read_frame`, or
straight to `read_rollout` as its `cursor`, with no arithmetic in between.

**A drilldown, not a dump.** `list_sessions` finds the session, `list_turns` finds the
exchange, and then one of the two readings, one row-bounded page at a time. What a reader's
context can afford is the reader's own concern — the console returns rows whole and lets the
harness or MCP client truncate for itself.

**Every page has the same shape**, and that is load-bearing rather than cosmetic: `Page` is
`items` plus `next_cursor`, every tool that pages takes the cursor back as `cursor`, and a cursor
always names **the first item the page did not return**. So one loop reads any of them. What a
cursor *is* differs, because the underlying keys genuinely differ — a session keyset
`(created_at, session_id)`, a frame sequence, a turn keyset, a transcript ordinal — and each is
its own type with its own fields rather than an opaque string pretending they are one key. A page
whose shape matched but whose cursor lied about the order underneath would be worse than the
heterogeneity it replaced.

**`list_` finds, `read_` reads.** `list_sessions` and `list_turns` are inventories: rows you
scan to pick the one worth opening, each carrying enough accounting to choose. `read_transcript`,
`read_rollout` and `read_frame` return the thing itself. The split is not paged-versus-whole —
`read_rollout` and `read_transcript` page too.

**Reading is a cursor over the log; turns are an index into it.** `frame_seq` already totally
orders a session, so `read_rollout` needs no notion of a turn — and a turn is the console's
interpretation rather than the record, since the CLI folds a mid-turn prompt into the running
turn and one `result` can then answer two prompts. `list_turns` therefore reports each exchange
as a *range* over the same log, with what it cost and how it ended: enough to pick the exchange
worth reading, without the frames themselves being reshaped around it.

**The records are the store's; the pages are this server's.** What a read produced — a session,
a frame, a turn, a transcript entry, and the cursors that walk them — is defined beside the store
that produces it (`haku/console/x/conversation_records.py`). What is here is how those records
are handed out: the `Page` envelope.

**Reads require the configured in-process-server grant.** The outer Console MCP boundary places
the revalidated caller in trusted request metadata; it is never supplied by tool arguments.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated, Literal, Protocol
from uuid import UUID

from fastmcp import FastMCP
from fastmcp.dependencies import Depends
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, Field

from haku.console.chat_models import BridgeFrameKind
from haku.console.in_process_server_access import InProcessServerAccessPolicy
from haku.console.mcp_execution import McpExecutionContext, require_mcp_execution_context
from haku.console.x.conversation_records import (
    FrameCursor,
    RolloutFrame,
    SessionCursor,
    SessionRecord,
    TranscriptCursor,
    TranscriptEntry,
    TranscriptSlice,
    TurnCursor,
    TurnRecord,
)

HAKU_CONVERSATIONS_SERVER_ID = "haku_conversations"
_EXECUTION_CONTEXT_DEPENDENCY = Depends(require_mcp_execution_context)

# Rows per page. Small on purpose: a frame carries a whole tool result, and the console's
# past-tool-calls page already learned that asking for hundreds of such rows means a
# multi-megabyte response (<../README.md> § Past tool calls).
MAX_PAGE = 100
DEFAULT_PAGE = 25

# Haku's own outer frame classes. Native harness discriminators remain inside opaque JSON and are
# deliberately not an argument vocabulary here.
FrameKind = Literal["harness_frame", "setup_output"]


class Page[ItemT, CursorT](BaseModel):
    """One page of a listing, and where the next one starts.

    Every paged tool here returns this shape, so a caller writes one loop: read `items`, and if
    `next_cursor` is present pass it back as `cursor`. The cursor type is the tool's own, because
    the four listings are ordered by four genuinely different keys.
    """

    items: list[ItemT]
    next_cursor: CursorT | None = Field(
        description="The first item this page did not return. Pass it back as `cursor` to continue; "
        "absent when this page is the last."
    )


class SessionPage(Page[SessionRecord, SessionCursor]):
    pass


class RolloutPage(Page[RolloutFrame, FrameCursor]):
    pass


class TurnPage(Page[TurnRecord, TurnCursor]):
    pass


class TranscriptPage(Page[TranscriptEntry, TranscriptCursor]):
    unreadable: dict[str, int] | None = Field(
        default=None,
        description="Records in the conversation log this release has no reading for, by their stored kind, "
        "counted over the whole session rather than this page — so a transcript that is quietly missing "
        "something says so. These were written by a newer console release; this does not count harness frames, "
        "which the transcript is not read from. Absent when there were none.",
    )


class ConversationReader(Protocol):
    """The console's session store, as this server needs it.

    A port rather than an import: the store lives in the experimental chat runtime, and this
    server should not depend on that package's shape. The *records* it exchanges do come from
    there (<../x/conversation_records.py>), because the store is what produces them — but that
    module is a leaf of models, so naming it pulls in no runtime.

    Every method answers "up to `limit` rows from `cursor`" and leaves the page to the caller.
    Two of the five tools also spend a byte budget the store knows nothing about, so the cut has
    to happen above it; doing it the same way for all of them is what keeps one shape.
    """

    async def list_sessions(self, *, cursor: SessionCursor | None, limit: int) -> list[SessionRecord]: ...

    async def read_frames(
        self,
        session_id: UUID,
        *,
        cursor: FrameCursor | None,
        limit: int,
        kinds: Sequence[BridgeFrameKind] | None = None,
    ) -> list[RolloutFrame]: ...

    async def read_frame(self, session_id: UUID, frame_seq: int) -> RolloutFrame | None: ...

    async def list_turns(self, session_id: UUID, *, cursor: TurnCursor | None, limit: int) -> list[TurnRecord]: ...

    async def read_transcript(
        self, session_id: UUID, *, cursor: TranscriptCursor | None, limit: int
    ) -> TranscriptSlice: ...


def split_page[ItemT](rows: Sequence[ItemT], *, limit: int) -> tuple[list[ItemT], ItemT | None]:
    """The page, and the first row it did not return — which is what every cursor here names.

    `rows` is one longer than `limit` when more exist, which is how a page knows it is not the
    last without a second count query.
    """
    return list(rows[:limit]), rows[limit] if len(rows) > limit else None


def build_mcp(reader: ConversationReader, *, access: InProcessServerAccessPolicy) -> FastMCP:
    mcp: FastMCP = FastMCP(
        name=HAKU_CONVERSATIONS_SERVER_ID,
        instructions=(
            "Read Haku's past sessions: start with `list_sessions`, then `list_turns`, then "
            "`read_transcript`. Follow transcript `provenance` into `read_rollout`/`read_frame` when "
            "normalization needs checking. Every listing returns `items` and `next_cursor`; pass the "
            "cursor back as `cursor`. Read-only."
        ),
    )

    def require_conversation_access(execution: McpExecutionContext) -> None:
        if not access.allows(execution.caller, HAKU_CONVERSATIONS_SERVER_ID):
            raise ToolError("in-process server access denied")

    @mcp.tool
    async def list_sessions(
        cursor: Annotated[
            SessionCursor | None,
            Field(default=None, description="From a previous page's `next_cursor`; omit for the newest sessions."),
        ] = None,
        limit: Annotated[int, Field(default=20, ge=1, le=MAX_PAGE, description="Most recent sessions first.")] = 20,
        execution: McpExecutionContext = _EXECUTION_CONTEXT_DEPENDENCY,
    ) -> SessionPage:
        """List past sessions, newest first. Use `conversation_id` to group continuations."""
        require_conversation_access(execution)
        sessions, more = split_page(await reader.list_sessions(cursor=cursor, limit=limit + 1), limit=limit)
        return SessionPage(items=sessions, next_cursor=SessionCursor.of(more) if more is not None else None)

    @mcp.tool
    async def list_turns(
        session_id: Annotated[UUID, Field(description="From `list_sessions`.")],
        cursor: Annotated[
            TurnCursor | None,
            Field(default=None, description="From a previous page's `next_cursor`; omit for the newest exchanges."),
        ] = None,
        limit: Annotated[int, Field(default=DEFAULT_PAGE, ge=1, le=MAX_PAGE, description="Newest exchange first.")] = (
            DEFAULT_PAGE
        ),
        execution: McpExecutionContext = _EXECUTION_CONTEXT_DEPENDENCY,
    ) -> TurnPage:
        """List a session's exchanges with cost, duration, outcome, and frame range."""
        require_conversation_access(execution)
        turns, more = split_page(await reader.list_turns(session_id, cursor=cursor, limit=limit + 1), limit=limit)
        return TurnPage(items=turns, next_cursor=TurnCursor.of(more) if more is not None else None)

    @mcp.tool
    async def read_transcript(
        session_id: Annotated[UUID, Field(description="From `list_sessions`.")],
        cursor: Annotated[
            TranscriptCursor | None,
            Field(default=None, description="From a previous page's `next_cursor`; omit to start at the beginning."),
        ] = None,
        limit: Annotated[int, Field(default=DEFAULT_PAGE, ge=1, le=MAX_PAGE)] = DEFAULT_PAGE,
        execution: McpExecutionContext = _EXECUTION_CONTEXT_DEPENDENCY,
    ) -> TranscriptPage:
        """Read normalized prompts, messages, reasoning, tool calls, and results oldest first.

        Entries use the console's neutral vocabulary and carry `provenance`; follow it into
        `read_frame` when a normalization needs checking. Tool calls and results are separate
        entries joined by `call_id`. A `prompt` entry says who asked in its `origin`: `harness`
        is the agent resuming its own session, which nobody typed.
        """
        require_conversation_access(execution)
        slice_ = await reader.read_transcript(session_id, cursor=cursor, limit=limit + 1)
        entries, more = split_page(slice_.entries, limit=limit)
        return TranscriptPage(
            items=entries,
            next_cursor=TranscriptCursor.of(more) if more is not None else None,
            unreadable=slice_.unreadable,
        )

    @mcp.tool
    async def read_rollout(
        session_id: Annotated[UUID, Field(description="From `list_sessions`.")],
        cursor: Annotated[
            FrameCursor | None,
            Field(
                default=None,
                description="Start at this `frame_seq`, inclusively — a previous page's `next_cursor`, or a "
                "transcript entry's `first_frame_seq`. Omit to start at the beginning of the log.",
            ),
        ] = None,
        execution: McpExecutionContext = _EXECUTION_CONTEXT_DEPENDENCY,
        limit: Annotated[int, Field(default=DEFAULT_PAGE, ge=1, le=MAX_PAGE)] = DEFAULT_PAGE,
        kinds: Annotated[
            list[FrameKind] | None,
            Field(
                default=None,
                description="Only these Haku bridge classes. Native harness frame shapes are not classified here.",
            ),
        ] = None,
    ) -> RolloutPage:
        """Read a session's native harness frames in order, rather than its normalized transcript."""
        require_conversation_access(execution)
        frames, more = split_page(
            await reader.read_frames(
                session_id,
                cursor=cursor,
                limit=limit + 1,
                kinds=None if kinds is None else [BridgeFrameKind(kind) for kind in kinds],
            ),
            limit=limit,
        )
        return RolloutPage(items=frames, next_cursor=FrameCursor.of(more) if more is not None else None)

    @mcp.tool
    async def read_frame(
        session_id: Annotated[UUID, Field(description="From `list_sessions`.")],
        frame_seq: Annotated[
            int, Field(description="From `read_rollout`, or from a transcript entry's `provenance.first_frame_seq`.")
        ],
        execution: McpExecutionContext = _EXECUTION_CONTEXT_DEPENDENCY,
    ) -> RolloutFrame:
        """Read one exact native frame, when a transcript normalization needs checking."""
        require_conversation_access(execution)
        frame = await reader.read_frame(session_id, frame_seq)
        if frame is None:
            raise ValueError(f"no such frame: {session_id=} {frame_seq=}")
        return frame

    return mcp
