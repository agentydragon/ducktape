"""haku-console's in-process `haku_conversations` MCP server — reading past sessions.

Lets Haku consult what it actually did in an earlier session, rather than starting each one
from the last twenty room messages and nothing else: the room is not the only corpus and it is the
smaller one (<../x/channels/matrix/SPEC.md> § The agent's own view). What it reads are the console's
two durable records: the conversation — neutral events folded into items as each frame arrived —
and `session_frames`, the verbatim wire they were read off. A tool call and the result it got
are in both — which the room is not.

**Each read is named and keyed by the layer it reads** (<../docs/chat_layers.md>): a session has
frames; a conversation has events, which fold into items. `read_conversation_items` is what a conversation
*meant* — prompts, messages, reasoning, tool calls and their results, as one vocabulary that says
nothing about which agent backend produced them, keyed by the conversation because the thread
outlives every session that ran it. `read_frames` is the frames a **named** backend actually sent,
verbatim — keyed by the session, because frames are one runner's wire and die with its provider
shape. The first is what a reader almost always wants; the second is the appeal, and every item
entry carries the session and frame range to appeal to (`provenance`). That path is the whole
reason an entry records where it came from, so it is built to be walked: a `frames` provenance
hands `session_id` and `first_frame_seq` straight to `read_frames` as its `cursor`, with no
arithmetic in between.

**A drilldown, not a dump.** `list_sessions` finds the thread, `list_turns` finds the
exchange, and then one of the two readings. Context is the scarce resource, so a page is bounded
in rows *and* in bytes — it stops when either runs out and its cursor says where.

**Every page has the same shape**, and that is load-bearing rather than cosmetic: `Page` is
`items` plus `next_cursor`, every tool that pages takes the cursor back as `cursor`, and a cursor
always names **the first item the page did not return**. So one loop reads any of them. What a
cursor *is* differs, because the underlying keys genuinely differ — a session keyset
`(created_at, session_id)`, a frame sequence, a turn keyset, a conversation stream position — and
each is its own type with its own fields rather than an opaque string pretending they are one key.
A page whose shape matched but whose cursor lied about the order underneath would be worse than
the heterogeneity it replaced.

**`list_` finds, `read_` reads.** `list_sessions` and `list_turns` are inventories: rows you
scan to pick the one worth opening, each carrying enough accounting to choose. `read_conversation_items` and
`read_frames` return the thing itself. The split is not paged-versus-whole — the reads page too.

**Every frame is recorded whole**, so a page's byte budget protects nothing but the reader's
context — which is why it is a budget on the page rather than a cap on each frame. The earlier
per-frame cap got that wrong in both directions: it dropped frames a page had ample room for, and
still allowed a page of many just under the line. The census of production frames was blind to
every `control_response` in the corpus for exactly that reason. A `limit=1` page is the escape
hatch the budget leaves open: a caller naming one row gets it whole, however large.

**Reading is a cursor over the log; turns are an index into it.** `frame_seq` already totally
orders a session, so `read_frames` needs no notion of a turn — and a turn is the console's
interpretation rather than the record, since the CLI folds a mid-turn prompt into the running
turn and one `result` can then answer two prompts. `list_turns` therefore reports each exchange
as a *range* over the same log, with what it cost and how it ended: enough to pick the exchange
worth reading, without the frames themselves being reshaped around it.

**The records are the store's; the pages are this server's.** What a read produced — a session,
a frame, a turn, a conversation entry, and the cursors that walk them — is defined beside the
store that produces it (`haku/console/x/conversation_records.py`). What is here is how those
records are handed out: the `Page` envelope, the byte budget a page spends, and the clipping that
budget forces.

**Reads require the configured in-process-server grant.** The outer Console MCP boundary places
the revalidated caller in trusted request metadata; it is never supplied by tool arguments.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
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
    ConversationEntry,
    FrameCursor,
    FrameRecord,
    ItemCursor,
    SessionCursor,
    SessionRecord,
    ToolResultEntry,
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

# Bytes of payload one page will hand back before it stops and points its cursor at where it
# stopped. A row limit alone does not bound a response, because one `tool_result` can be an entire
# file — as a frame, and equally as the `structured` half of an item entry.
#
# **The budget is on the page, not on the row.** Every frame is recorded whole, so there is
# nothing to protect but the reader's context — and a reader's context is spent by the response,
# not by any one row in it. A per-frame cap gets that wrong in both directions at once: it dropped
# a 9 KB frame that a page had ample room for (21% of production `user` frames, every
# `control_response`, effectively every `system/init`),
# while still permitting a page of 25 frames just under the line. Stopping the page instead means
# a large row costs the rest of its page rather than its own contents, and the cursor already
# says where to resume.
#
# 200 KB because that was the old regime's worst case (25 × 8 KB), so the ceiling on a response
# does not move — only what a reader can spend it on.
MAX_PAGE_BYTES = 200_000


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


class FramePage(Page[FrameRecord, FrameCursor]):
    pass


class TurnPage(Page[TurnRecord, TurnCursor]):
    pass


class ItemPage(Page[ConversationEntry, ItemCursor]):
    pass


class ConversationReader(Protocol):
    """The console's session store, as this server needs it.

    A port rather than an import: the store lives in the experimental chat runtime, and this
    server should not depend on that package's shape. The *records* it exchanges do come from
    there (<../x/conversation_records.py>), because the store is what produces them — but that
    module is a leaf of models, so naming it pulls in no runtime.

    Every method answers "up to `limit` rows from `cursor`" and leaves the page to the caller.
    Two of the four tools also spend a byte budget the store knows nothing about, so the cut has
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
    ) -> list[FrameRecord]: ...

    async def list_turns(self, session_id: UUID, *, cursor: TurnCursor | None, limit: int) -> list[TurnRecord]: ...

    async def read_conversation_items(
        self, conversation_id: UUID, *, cursor: ItemCursor | None, limit: int
    ) -> list[ConversationEntry]: ...


def payload_bytes(frame: FrameRecord) -> int:
    return 0 if frame.payload is None else len(json.dumps(frame.payload))


def clip_frame(frame: FrameRecord) -> FrameRecord:
    """Drop a payload, recording what was there.

    Clipping rather than truncating the JSON: half an object is not parseable and reads as
    corruption, where a stated size and a missing payload is a fact the caller can act on —
    a `limit=1` page at this frame reads it whole.
    """
    return frame.model_copy(update={"payload": None, "clipped_bytes": payload_bytes(frame)})


def entry_bytes(entry: ConversationEntry) -> int:
    return len(entry.model_dump_json())


def clip_entry(entry: ConversationEntry) -> ConversationEntry:
    """Drop a tool result's structured payload — the half that is routinely a whole file.

    Any other entry goes out whole: nothing else here has an unbounded field, and an entry that is
    large without one is a very long message, which clipping would leave nothing of. `provenance`
    is on every entry either way, so the frames behind it are always readable.
    """
    if isinstance(entry, ToolResultEntry) and entry.structured is not None:
        return entry.model_copy(
            update={"structured": None, "clipped_bytes": len(json.dumps(entry.structured, default=str))}
        )
    return entry


def split_page[ItemT](rows: Sequence[ItemT], *, limit: int) -> tuple[list[ItemT], ItemT | None]:
    """The page, and the first row it did not return — which is what every cursor here names.

    `rows` is one longer than `limit` when more exist, which is how a page knows it is not the
    last without a second count query.
    """
    return list(rows[:limit]), rows[limit] if len(rows) > limit else None


def take_page[ItemT](
    rows: Sequence[ItemT], *, limit: int, size: Callable[[ItemT], int], clip: Callable[[ItemT], ItemT]
) -> tuple[list[ItemT], ItemT | None]:
    """`split_page` under a byte budget: as many rows as fit, and the first one that did not."""
    page: list[ItemT] = []
    spent = 0
    for index, row in enumerate(rows[:limit]):
        cost = size(row)
        if not page and cost > MAX_PAGE_BYTES:
            # One row larger than the entire budget. It has to go out: skipping it would leave the
            # cursor unable to advance past it, and a reader looping forever. A page of one is a
            # whole-row read — the caller named this row, so the budget yields to it; a wider page
            # clips it instead and the caller re-asks with `limit=1`.
            served = row if limit == 1 else clip(row)
            return [served], rows[index + 1] if len(rows) > index + 1 else None
        if spent + cost > MAX_PAGE_BYTES:
            return page, row
        page.append(row)
        spent += cost
    return split_page(rows, limit=limit)


def build_mcp(reader: ConversationReader, *, access: InProcessServerAccessPolicy) -> FastMCP:
    mcp: FastMCP = FastMCP(
        name=HAKU_CONVERSATIONS_SERVER_ID,
        instructions=(
            "Read Haku's past conversations: start with `list_sessions`, then `list_turns`, then "
            "`read_conversation_items`. Follow an entry's `provenance` into `read_frames` when normalization "
            "needs checking. Every listing returns `items` and `next_cursor`; pass the cursor back "
            "as `cursor`. Read-only."
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
    async def read_conversation_items(
        conversation_id: Annotated[UUID, Field(description="From `list_sessions`; the thread, not one runner's life.")],
        cursor: Annotated[
            ItemCursor | None,
            Field(default=None, description="From a previous page's `next_cursor`; omit to start at the beginning."),
        ] = None,
        limit: Annotated[int, Field(default=DEFAULT_PAGE, ge=1, le=MAX_PAGE)] = DEFAULT_PAGE,
        execution: McpExecutionContext = _EXECUTION_CONTEXT_DEPENDENCY,
    ) -> ItemPage:
        """Read the conversation's prompts, messages, reasoning, tool calls, and results oldest first.

        The whole thread, across replaced sessions: a conversation outlives its runners, and this
        read does not stop where a sandbox died. Entries use the console's neutral vocabulary and
        carry `provenance`; follow it into `read_frames` when a normalization needs checking. Tool
        calls and results are separate entries joined by `call_id`. A `prompt` entry says who asked
        in its `origin`: `harness` is the agent resuming its own session, which nobody typed. A
        `clipped_bytes` entry is re-read whole by a `limit=1` page at its `seq`.
        """
        require_conversation_access(execution)
        entries, more = take_page(
            await reader.read_conversation_items(conversation_id, cursor=cursor, limit=limit + 1),
            limit=limit,
            size=entry_bytes,
            clip=clip_entry,
        )
        return ItemPage(items=entries, next_cursor=ItemCursor.of(more) if more is not None else None)

    @mcp.tool
    async def read_frames(
        session_id: Annotated[UUID, Field(description="From `list_sessions`, or an entry's `provenance.session_id`.")],
        cursor: Annotated[
            FrameCursor | None,
            Field(
                default=None,
                description="Start at this `frame_seq`, inclusively — a previous page's `next_cursor`, or an "
                "entry's `first_frame_seq`. Omit to start at the beginning of the log.",
            ),
        ] = None,
        execution: McpExecutionContext = _EXECUTION_CONTEXT_DEPENDENCY,
        limit: Annotated[
            int,
            Field(
                default=DEFAULT_PAGE,
                ge=1,
                le=MAX_PAGE,
                description="With `limit=1` the named frame comes back whole however large — use it when "
                "`clipped_bytes` is present.",
            ),
        ] = DEFAULT_PAGE,
        kinds: Annotated[
            list[FrameKind] | None,
            Field(
                default=None,
                description="Only these Haku bridge classes. Native harness frame shapes are not classified here.",
            ),
        ] = None,
    ) -> FramePage:
        """Read a session's native harness frames in order, rather than its normalized items."""
        require_conversation_access(execution)
        frames, more = take_page(
            await reader.read_frames(
                session_id,
                cursor=cursor,
                limit=limit + 1,
                kinds=None if kinds is None else [BridgeFrameKind(kind) for kind in kinds],
            ),
            limit=limit,
            size=payload_bytes,
            clip=clip_frame,
        )
        return FramePage(items=frames, next_cursor=FrameCursor.of(more) if more is not None else None)

    return mcp
