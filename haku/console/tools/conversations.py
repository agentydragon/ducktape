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
exchange, and then one of the two readings, one row-bounded page at a time. Rows go out whole —
what a reader's context can afford is the harness's or MCP client's concern, not the console's.

**Every page has the same shape**, and that is load-bearing rather than cosmetic: `Page` is
`items` plus `next_cursor`, every tool that pages takes the cursor back as `cursor`, and a cursor
always names **the first item the page did not return**. So one loop reads any of them. What a
cursor *is* differs, because the underlying keys genuinely differ — a session keyset
`(created_at, session_id)`, a frame sequence, a turn keyset, a conversation stream position. The
single-column keys are plain integers; the composite keysets are their own types with their own
fields rather than opaque strings pretending they are one key. A page whose shape matched but
whose cursor lied about the order underneath would be worse than the heterogeneity it replaced.

**`list_` finds, `read_` reads.** `list_sessions` and `list_turns` are inventories: rows you
scan to pick the one worth opening, each carrying enough accounting to choose. `read_conversation_items` and
`read_frames` return the thing itself. The split is not paged-versus-whole — the reads page too.

**Reading is a cursor over the log; turns are an index into it.** `frame_seq` already totally
orders a session, so `read_frames` needs no notion of a turn — and a turn is the console's
interpretation rather than the record, since the CLI folds a mid-turn prompt into the running
turn and one `result` can then answer two prompts. `list_turns` therefore reports each exchange
as a *range* over the same log, with what it cost and how it ended: enough to pick the exchange
worth reading, without the frames themselves being reshaped around it.

**The read models are the store's; the pages are this server's.** What a read produced — a
session, a frame, a turn, a conversation entry, and the cursors that walk them — is defined
beside the store that produces it (`haku/console/x/conversation_reads.py`). What is here is how
those reads are handed out: the `Page` envelope.

**Reads require the configured in-process-server grant, and rows require the profile-DAG scope.**
The outer Console MCP boundary places the revalidated caller in trusted request metadata; it is
never supplied by tool arguments. The server grant admits the caller at all; which conversations
it then sees is `conversation_read_access`'s decision, the same one semantic Recall applies —
`list_sessions` is filtered to the caller's readable profiles, and naming a session or
conversation outside them is refused as `conversation access denied`.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated, Literal, Protocol
from uuid import UUID

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, Field

from haku.console.chat_models import BridgeFrameKind
from haku.console.conversation_read_access import (
    ConversationAccessDeniedError,
    ConversationReadAccessPolicy,
    ConversationReadScope,
)
from haku.console.in_process_server_access import InProcessServerAccessPolicy
from haku.console.mcp_execution import EXECUTION_CONTEXT_DEPENDENCY, McpExecutionContext
from haku.console.x.conversation_reads import (
    ConversationEntry,
    FrameRecord,
    SessionCursor,
    SessionRecord,
    TurnCursor,
    TurnRecord,
)

HAKU_CONVERSATIONS_SERVER_ID = "haku_conversations"

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


class FramePage(Page[FrameRecord, int]):
    pass


class TurnPage(Page[TurnRecord, TurnCursor]):
    pass


class ItemPage(Page[ConversationEntry, int]):
    pass


class ConversationReader(Protocol):
    """The console's session store, as this server needs it.

    A port rather than an import: the store lives in the experimental chat runtime, and this
    server should not depend on that package's shape. The *records* it exchanges do come from
    there (<../x/conversation_reads.py>), because the store is what produces them — but that
    module is a leaf of models, so naming it pulls in no runtime.

    Every method answers "up to `limit` rows from `cursor`" and leaves the page to the caller.
    Two of the four tools also spend a byte budget the store knows nothing about, so the cut has
    to happen above it; doing it the same way for all of them is what keeps one shape.
    """

    async def list_sessions(
        self, *, cursor: SessionCursor | None, limit: int, scope: ConversationReadScope
    ) -> list[SessionRecord]: ...

    async def read_frames(
        self,
        session_id: UUID,
        *,
        cursor: int | None,
        limit: int,
        scope: ConversationReadScope,
        kinds: Sequence[BridgeFrameKind] | None = None,
    ) -> list[FrameRecord]: ...

    async def list_turns(
        self, session_id: UUID, *, cursor: TurnCursor | None, limit: int, scope: ConversationReadScope
    ) -> list[TurnRecord]: ...

    async def read_conversation_items(
        self, conversation_id: UUID, *, cursor: int | None, limit: int, scope: ConversationReadScope
    ) -> list[ConversationEntry]: ...


def split_page[ItemT](rows: Sequence[ItemT], *, limit: int) -> tuple[list[ItemT], ItemT | None]:
    """The page, and the first row it did not return — which is what every cursor here names.

    `rows` is one longer than `limit` when more exist, which is how a page knows it is not the
    last without a second count query.
    """
    return list(rows[:limit]), rows[limit] if len(rows) > limit else None


def build_mcp(
    reader: ConversationReader, *, access: InProcessServerAccessPolicy, conversation_reads: ConversationReadAccessPolicy
) -> FastMCP:
    mcp: FastMCP = FastMCP(
        name=HAKU_CONVERSATIONS_SERVER_ID,
        instructions=(
            "Read Haku's past conversations: start with `list_sessions`, then `list_turns`, then "
            "`read_conversation_items`. Follow an entry's `provenance` into `read_frames` when normalization "
            "needs checking. Every listing returns `items` and `next_cursor`; pass the cursor back "
            "as `cursor`. Read-only."
        ),
    )

    def read_scope(execution: McpExecutionContext) -> ConversationReadScope:
        """The server grant admits the caller; the profile-DAG scope decides which rows it sees."""
        if not access.allows(execution.caller, HAKU_CONVERSATIONS_SERVER_ID):
            raise ToolError("in-process server access denied")
        return conversation_reads.scope_for(execution.caller)

    @mcp.tool
    async def list_sessions(
        cursor: Annotated[
            SessionCursor | None,
            Field(default=None, description="From a previous page's `next_cursor`; omit for the newest sessions."),
        ] = None,
        limit: Annotated[int, Field(default=20, ge=1, le=MAX_PAGE, description="Most recent sessions first.")] = 20,
        execution: McpExecutionContext = EXECUTION_CONTEXT_DEPENDENCY,
    ) -> SessionPage:
        """List past sessions, newest first. Use `conversation_id` to group continuations."""
        scope = read_scope(execution)
        sessions, more = split_page(
            await reader.list_sessions(cursor=cursor, limit=limit + 1, scope=scope), limit=limit
        )
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
        execution: McpExecutionContext = EXECUTION_CONTEXT_DEPENDENCY,
    ) -> TurnPage:
        """List a session's exchanges with cost, duration, outcome, and frame range."""
        scope = read_scope(execution)
        try:
            rows = await reader.list_turns(session_id, cursor=cursor, limit=limit + 1, scope=scope)
        except ConversationAccessDeniedError:
            raise ToolError("conversation access denied") from None
        turns, more = split_page(rows, limit=limit)
        return TurnPage(items=turns, next_cursor=TurnCursor.of(more) if more is not None else None)

    @mcp.tool
    async def read_conversation_items(
        conversation_id: Annotated[UUID, Field(description="From `list_sessions`; the thread, not one runner's life.")],
        cursor: Annotated[
            int | None,
            Field(
                default=None,
                description="A previous page's `next_cursor` — the stream position (`seq`) of the first entry "
                "not yet returned, inclusively. Omit to start at the beginning.",
            ),
        ] = None,
        limit: Annotated[int, Field(default=DEFAULT_PAGE, ge=1, le=MAX_PAGE)] = DEFAULT_PAGE,
        execution: McpExecutionContext = EXECUTION_CONTEXT_DEPENDENCY,
    ) -> ItemPage:
        """Read the conversation's prompts, messages, reasoning, tool calls, and results oldest first.

        The whole thread, across replaced sessions: a conversation outlives its runners, and this
        read does not stop where a sandbox died. Entries use the console's neutral vocabulary and
        carry `provenance`; follow it into `read_frames` when a normalization needs checking. Tool
        calls and results are separate entries joined by `call_id`. A `prompt` entry says who asked
        in its `origin`: `harness` is the agent resuming its own session, which nobody typed.
        """
        scope = read_scope(execution)
        try:
            rows = await reader.read_conversation_items(conversation_id, cursor=cursor, limit=limit + 1, scope=scope)
        except ConversationAccessDeniedError:
            raise ToolError("conversation access denied") from None
        entries, more = split_page(rows, limit=limit)
        return ItemPage(items=entries, next_cursor=more.seq if more is not None else None)

    @mcp.tool
    async def read_frames(
        session_id: Annotated[UUID, Field(description="From `list_sessions`, or an entry's `provenance.session_id`.")],
        cursor: Annotated[
            int | None,
            Field(
                default=None,
                description="Start at this `frame_seq`, inclusively — a previous page's `next_cursor`, or an "
                "entry's `first_frame_seq`. Omit to start at the beginning of the log; `limit=1` reads exactly "
                "the named frame.",
            ),
        ] = None,
        execution: McpExecutionContext = EXECUTION_CONTEXT_DEPENDENCY,
        limit: Annotated[int, Field(default=DEFAULT_PAGE, ge=1, le=MAX_PAGE)] = DEFAULT_PAGE,
        kinds: Annotated[
            list[FrameKind] | None,
            Field(
                default=None,
                description="Only these outer frame classes. Native harness frame shapes are not classified here.",
            ),
        ] = None,
    ) -> FramePage:
        """Read a session's native harness frames in order, rather than its normalized items."""
        scope = read_scope(execution)
        try:
            rows = await reader.read_frames(
                session_id,
                cursor=cursor,
                limit=limit + 1,
                scope=scope,
                kinds=None if kinds is None else [BridgeFrameKind(kind) for kind in kinds],
            )
        except ConversationAccessDeniedError:
            raise ToolError("conversation access denied") from None
        frames, more = split_page(rows, limit=limit)
        return FramePage(items=frames, next_cursor=more.frame_seq if more is not None else None)

    return mcp
