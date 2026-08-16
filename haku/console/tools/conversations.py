"""haku-console's in-process `haku_conversations` MCP server — reading past sessions.

Lets Haku consult what it actually did in an earlier session, rather than starting each one
from the last twenty room messages and nothing else (`haku/plans/matrix_chat_runtime.md` R5.4a,
Phase 5). The corpus is `session_frames`, the console's verbatim record of the agent
protocol, so a tool call and the result it got are both there — which no other table has.

**Two readings of one corpus, and the drilldown runs between them.** `read_transcript` is what a
conversation *meant* — messages, reasoning, tool calls and their results, as one vocabulary that
says nothing about which agent backend produced them. `read_rollout` and `read_frame` are the
provider's own protocol frames, verbatim. The first is what a reader almost always wants; the
second is the appeal, and every transcript entry carries the frame range to appeal to
(`provenance`). That path is the whole reason the transcript records where it came from, so it is
built to be walked: a `frames` provenance hands `first_frame_seq` straight to `read_frame`, or
straight to `read_rollout` as its `cursor`, with no arithmetic in between.

**A drilldown, not a dump.** `list_conversations` finds the session, `list_turns` finds the
exchange, and then one of the two readings. Context is the scarce resource, so a page is bounded
in rows *and* in bytes — it stops when either runs out and its cursor says where.

**Every page has the same shape**, and that is load-bearing rather than cosmetic: `Page` is
`items` plus `next_cursor`, every tool that pages takes the cursor back as `cursor`, and a cursor
always names **the first item the page did not return**. So one loop reads any of them. What a
cursor *is* differs, because the underlying keys genuinely differ — a session keyset
`(created_at, session_id)`, a frame sequence, a turn keyset, a transcript ordinal — and each is
its own type with its own fields rather than an opaque string pretending they are one key. A page
whose shape matched but whose cursor lied about the order underneath would be worse than the
heterogeneity it replaced.

**`list_` finds, `read_` reads.** `list_conversations` and `list_turns` are inventories: rows you
scan to pick the one worth opening, each carrying enough accounting to choose. `read_transcript`,
`read_rollout` and `read_frame` return the thing itself. The split is not paged-versus-whole —
`read_rollout` and `read_transcript` page too.

**Every frame is recorded whole**, so a page's byte budget protects nothing but the reader's
context — which is why it is a budget on the page rather than a cap on each frame. The earlier
per-frame cap got that wrong in both directions: it dropped frames a page had ample room for, and
still allowed a page of many just under the line. The census of production frames was blind to
every `control_response` in the corpus for exactly that reason.

**Reading is a cursor over the log; turns are an index into it.** `frame_seq` already totally
orders a session, so `read_rollout` needs no notion of a turn — and a turn is the console's
interpretation rather than the record, since the CLI folds a mid-turn prompt into the running
turn and one `result` can then answer two prompts. `list_turns` therefore reports each exchange
as a *range* over the same log, with what it cost and how it ended: enough to pick the exchange
worth reading, without the frames themselves being reshaped around it.

**The records are the store's; the pages are this server's.** What a read produced — a session,
a frame, a turn, a transcript entry, and the cursors that walk them — is defined beside the store
that produces it (`haku/console/x/conversation_records.py`). What is here is how those records
are handed out: the `Page` envelope, the byte budget a page spends, and the clipping that budget
forces.

**Reads are unscoped** (R5.3a): any session, whichever room it served. Deliberate for now —
the eventual policy about which Haku may read which past conversation is not settled, and
guessing at one here would be a scoping rule nobody stated.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from typing import Annotated, Literal, Protocol, get_args
from uuid import UUID

from fastmcp import FastMCP
from pydantic import BaseModel, Field

from haku.console.x.conversation_records import (
    Conversation,
    ConversationCursor,
    FrameCursor,
    RolloutFrame,
    ToolResultEntry,
    TranscriptCursor,
    TranscriptEntry,
    TranscriptSlice,
    TurnCursor,
    TurnRecord,
)

HAKU_CONVERSATIONS_SERVER_ID = "haku_conversations"

# Rows per page. Small on purpose: a frame carries a whole tool result, and the console's
# past-tool-calls page already learned that asking for hundreds of such rows means a
# multi-megabyte response (haku/console/debug/past_tool_calls_perf.md).
MAX_PAGE = 100
DEFAULT_PAGE = 25

# Bytes of payload one page will hand back before it stops and points its cursor at where it
# stopped. A row limit alone does not bound a response, because one `tool_result` can be an entire
# file — as a frame, and equally as the `structured` half of a transcript entry.
#
# **The budget is on the page, not on the row.** Every frame is recorded whole, so there is
# nothing to protect but the reader's context — and a reader's context is spent by the response,
# not by any one row in it. A per-frame cap gets that wrong in both directions at once: it dropped
# a 9 KB frame that a page had ample room for (21% of production `user` frames, every
# `control_response`, effectively every `system/init` — see `../debug/frame_shape_census.md`),
# while still permitting a page of 25 frames just under the line. Stopping the page instead means
# a large row costs the rest of its page rather than its own contents, and the cursor already
# says where to resume.
#
# 200 KB because that was the old regime's worst case (25 × 8 KB), so the ceiling on a response
# does not move — only what a reader can spend it on.
MAX_PAGE_BYTES = 200_000

# What a session's `kind` column actually holds, so a caller can filter for any of it. Every
# entry was observed in production; four of them are absent from the CLI's `protocol.md`, and
# `setup_output` is the console's own (`haku/console/x/setup_output.py`). Spelled here because
# it is this tool's argument vocabulary — an enum in the generated schema — rather than anything
# the store constrains: `read_frames` takes whatever strings it is handed.
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


class ConversationPage(Page[Conversation, ConversationCursor]):
    pass


class RolloutPage(Page[RolloutFrame, FrameCursor]):
    pass


class TurnPage(Page[TurnRecord, TurnCursor]):
    pass


class TranscriptPage(Page[TranscriptEntry, TranscriptCursor]):
    unreadable: dict[str, int] | None = Field(
        default=None,
        description="Frame classes this release has no reading for, counted over the whole session rather than "
        "this page — so a transcript that is quietly missing something says so. Absent when there were none. "
        "`read_rollout` is how to see what they held.",
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

    async def list_conversations(self, *, cursor: ConversationCursor | None, limit: int) -> list[Conversation]: ...

    # `Sequence` rather than `list` so the tool can narrow `kinds` to a Literal union for its
    # generated schema: `list` is invariant, so `list[Literal[...]]` would not satisfy `list[str]`.
    async def read_frames(
        self, session_id: UUID, *, cursor: FrameCursor | None, limit: int, kinds: Sequence[str] | None
    ) -> list[RolloutFrame]: ...

    async def list_turns(self, session_id: UUID, *, cursor: TurnCursor | None, limit: int) -> list[TurnRecord]: ...

    async def read_transcript(
        self, session_id: UUID, *, cursor: TranscriptCursor | None, limit: int
    ) -> TranscriptSlice: ...


def payload_bytes(frame: RolloutFrame) -> int:
    return 0 if frame.payload is None else len(json.dumps(frame.payload))


def clip_frame(frame: RolloutFrame) -> RolloutFrame:
    """Drop a payload, recording what was there.

    Clipping rather than truncating the JSON: half an object is not parseable and reads as
    corruption, where a stated size and a missing payload is a fact the caller can act on —
    `read_frame` reads it whole.
    """
    return frame.model_copy(update={"payload": None, "clipped_bytes": payload_bytes(frame)})


def entry_bytes(entry: TranscriptEntry) -> int:
    return len(entry.model_dump_json())


def clip_entry(entry: TranscriptEntry) -> TranscriptEntry:
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
            # One row larger than the entire budget. It has to go out clipped: skipping it would
            # leave the cursor unable to advance past it, and a reader looping forever.
            return [clip(row)], rows[index + 1] if len(rows) > index + 1 else None
        if spent + cost > MAX_PAGE_BYTES:
            return page, row
        page.append(row)
        spent += cost
    return split_page(rows, limit=limit)


def build_mcp(reader: ConversationReader) -> FastMCP:
    mcp: FastMCP = FastMCP(
        name=HAKU_CONVERSATIONS_SERVER_ID,
        instructions="Read Haku's own past sessions. `list_conversations` finds one and `list_turns` finds an "
        "exchange within it; `read_transcript` is what was said and done, in one vocabulary that names no "
        "agent backend, and `read_rollout` / `read_frame` are the provider's raw protocol frames behind it. "
        "Start with the transcript and follow an entry's `provenance` into the frames when a normalization "
        "looks wrong. Every listing pages the same way: pass `next_cursor` back as `cursor`. Read-only.",
    )

    @mcp.tool
    async def list_conversations(
        cursor: Annotated[
            ConversationCursor | None,
            Field(default=None, description="From a previous page's `next_cursor`; omit for the newest sessions."),
        ] = None,
        limit: Annotated[int, Field(default=20, ge=1, le=MAX_PAGE, description="Most recent sessions first.")] = 20,
    ) -> ConversationPage:
        """List Haku's past chat sessions, newest first, a page at a time.

        Keyset paging like every other listing here, not an offset: sessions keep being created
        at the top of this order while a reader walks it, so a page counted from the start would
        skip sessions or repeat them as new ones land.
        """
        conversations, more = split_page(await reader.list_conversations(cursor=cursor, limit=limit + 1), limit=limit)
        return ConversationPage(
            items=conversations, next_cursor=ConversationCursor.of(more) if more is not None else None
        )

    @mcp.tool
    async def list_turns(
        session_id: Annotated[UUID, Field(description="From `list_conversations`.")],
        cursor: Annotated[
            TurnCursor | None,
            Field(default=None, description="From a previous page's `next_cursor`; omit for the newest exchanges."),
        ] = None,
        limit: Annotated[int, Field(default=DEFAULT_PAGE, ge=1, le=MAX_PAGE, description="Newest exchange first.")] = (
            DEFAULT_PAGE
        ),
    ) -> TurnPage:
        """List a session's exchanges — what each cost, how long it took, how it ended.

        Each carries the frame range it produced, so this is the cheap way to find the exchange
        worth reading before reading it.
        """
        turns, more = split_page(await reader.list_turns(session_id, cursor=cursor, limit=limit + 1), limit=limit)
        return TurnPage(items=turns, next_cursor=TurnCursor.of(more) if more is not None else None)

    @mcp.tool
    async def read_transcript(
        session_id: Annotated[UUID, Field(description="From `list_conversations`.")],
        cursor: Annotated[
            TranscriptCursor | None,
            Field(default=None, description="From a previous page's `next_cursor`; omit to start at the beginning."),
        ] = None,
        limit: Annotated[int, Field(default=DEFAULT_PAGE, ge=1, le=MAX_PAGE)] = DEFAULT_PAGE,
    ) -> TranscriptPage:
        """Read what a conversation meant: messages, reasoning, tool calls and their results.

        In the console's own vocabulary rather than any agent backend's, so nothing here is
        `assistant`, a content block or a `tool_use_result`. Entries are in the order they
        happened, oldest first, and every one says which frames it was read off — follow that
        `provenance` into `read_frame` when a normalization looks wrong.

        **Deltas are not on this surface.** A conversation being read back is finished, and the
        increments of a message concatenate to exactly the `text` its `message` entry already
        carries — so streaming them here would double every answer for no information. A reader
        that genuinely wants the typing asks `read_rollout` for `stream_event` frames by name.

        A tool call and its result are two entries, joined by `call_id`, not one entry with the
        answer folded in: the call is real while it is still running, and a page that waited for
        the result would have to look arbitrarily far ahead or stop where it did not mean to.
        Activities pair the same way, by `activity_id`.
        """
        slice_ = await reader.read_transcript(session_id, cursor=cursor, limit=limit + 1)
        entries, more = take_page(slice_.entries, limit=limit, size=entry_bytes, clip=clip_entry)
        return TranscriptPage(
            items=entries,
            next_cursor=TranscriptCursor.of(more) if more is not None else None,
            unreadable=slice_.unreadable,
        )

    @mcp.tool
    async def read_rollout(
        session_id: Annotated[UUID, Field(description="From `list_conversations`.")],
        cursor: Annotated[
            FrameCursor | None,
            Field(
                default=None,
                description="Start at this `frame_seq`, inclusively — a previous page's `next_cursor`, or a "
                "transcript entry's `first_frame_seq`. Omit to start at the beginning of the log.",
            ),
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
        """Read one session's raw protocol frames in order, a page at a time.

        The provider's own wire format, verbatim. `read_transcript` is the same conversation
        already read; this is what to check it against.
        """
        frames, more = take_page(
            await reader.read_frames(session_id, cursor=cursor, limit=limit + 1, kinds=kinds),
            limit=limit,
            size=payload_bytes,
            clip=clip_frame,
        )
        return RolloutPage(items=frames, next_cursor=FrameCursor.of(more) if more is not None else None)

    @mcp.tool
    async def read_frame(
        session_id: Annotated[UUID, Field(description="From `list_conversations`.")],
        frame_seq: Annotated[
            int, Field(description="From `read_rollout`, or from a transcript entry's `provenance.first_frame_seq`.")
        ],
    ) -> RolloutFrame:
        """One frame in full, however large — including one too big for any page.

        A page spends a byte budget and stops, so a frame larger than the whole budget is the one
        thing it cannot hand over; naming a single frame bounds the response by that frame alone.
        Use it when a page returned `clipped_bytes` instead of a payload, and when a transcript
        entry's normalization needs checking against what actually arrived.

        Deltas (`stream_event`) are readable here too, but never need to be — one is a few
        characters of an answer.
        """
        # `kinds=None` would drop deltas from the query, and a caller naming a `frame_seq` has
        # already chosen its row.
        frames = await reader.read_frames(
            session_id, cursor=FrameCursor(frame_seq=frame_seq), limit=1, kinds=list(get_args(FrameKind))
        )
        if not frames or frames[0].frame_seq != frame_seq:
            raise ValueError(f"no such frame: {session_id=} {frame_seq=}")
        return frames[0]

    return mcp
