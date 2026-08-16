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

**Reads are unscoped** (R5.3a): any session, whichever room it served. Deliberate for now —
the eventual policy about which Haku may read which past conversation is not settled, and
guessing at one here would be a scoping rule nobody stated.
"""

from __future__ import annotations

import datetime
import json
from collections.abc import Callable, Sequence
from enum import StrEnum
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
# `setup_output` is the console's own (`haku/console/x/session_frames.py`). Spelled here rather
# than imported because this server is in the stable catalog and the vocabulary lives in the
# experimental chat runtime — the same reason `ConversationReader` is a port.
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


class Conversation(BaseModel):
    session_id: UUID
    surface: str | None = Field(description="`matrix` or `spa`; absent on sessions that predate the column.")
    room_id: str | None = Field(description="The Matrix room this session served, if it served one.")
    status: str
    created_at: datetime.datetime
    error: str | None = None


class ConversationCursor(BaseModel):
    """A position in the `(created_at, session_id)` order `list_conversations` walks.

    **Both columns, because one does not order the corpus.** Sessions are created in bursts — a
    Matrix room and the SPA can open one in the same instant, and `created_at` alone leaves that
    pair unordered. A cursor naming only the timestamp would then either hand back a session the
    previous page already carried or step over one it never did, depending on which side of the
    tie the database happened to return first. `session_id` breaks it, and the key is spelled out
    here rather than hidden behind an opaque string so a reader can see what the page boundary
    actually is.
    """

    created_at: datetime.datetime
    session_id: UUID

    @classmethod
    def of(cls, conversation: Conversation) -> ConversationCursor:
        return cls(created_at=conversation.created_at, session_id=conversation.session_id)


class ConversationPage(Page[Conversation, ConversationCursor]):
    pass


class RolloutFrame(BaseModel):
    frame_seq: int
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


class FrameCursor(BaseModel):
    """Where a read of the frame log starts — inclusively, so this is a frame that exists.

    Inclusive rather than "after this one" so that a transcript entry's `first_frame_seq` is
    already a cursor: appealing a normalization to the frames behind it needs no arithmetic, and
    an off-by-one there reads the wrong frame while looking right.
    """

    frame_seq: int

    @classmethod
    def of(cls, frame: RolloutFrame) -> FrameCursor:
        return cls(frame_seq=frame.frame_seq)


class RolloutPage(Page[RolloutFrame, FrameCursor]):
    pass


class TurnRecord(BaseModel):
    """One exchange of a session, as a range over that session's frames."""

    turn_id: UUID
    first_frame_seq: int = Field(description="Pass to `read_rollout` as `cursor` to read this exchange.")
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


class TurnCursor(BaseModel):
    """A position in the newest-first exchange order, tiebroken like `ConversationCursor`."""

    started_at: datetime.datetime
    turn_id: UUID

    @classmethod
    def of(cls, turn: TurnRecord) -> TurnCursor:
        return cls(started_at=turn.started_at, turn_id=turn.turn_id)


class TurnPage(Page[TurnRecord, TurnCursor]):
    pass


class FromFrames(BaseModel):
    """The frames this entry was read off, inclusive at both ends.

    Inclusive of everything between the ends, which is not the same as "these frames and no
    others": a message whose frames are interrupted by a tool result spans the interruption too,
    and that is the honest reading of a range rather than a defect in it.

    This is the appeal path. `read_frame(session_id, first_frame_seq)` returns the first one
    whole however large; `read_rollout(session_id, cursor={"frame_seq": first_frame_seq})` walks
    the span.
    """

    kind: Literal["frames"] = "frames"
    first_frame_seq: int
    last_frame_seq: int


class ConsoleAuthored(BaseModel):
    """The console said this itself, so there is no frame to appeal to and there never will be.

    Distinct in kind from a frame-derived entry whose range happens to be unknown: re-reading the
    frames can only preserve one of these, never re-derive it.
    """

    kind: Literal["authored"] = "authored"


type EntryProvenance = Annotated[FromFrames | ConsoleAuthored, Field(discriminator="kind")]


class Outcome(StrEnum):
    """How a step ended, where "cannot tell" is a first-class answer rather than a default.

    `UNKNOWN` is the common case, not the corner: the field a provider would report failure in is
    routinely absent, and collapsing that into `SUCCEEDED` reports every unanswerable case as
    fine. The shares are measured in <../debug/frame_shape_census.md>, which is dated; this is not.

    Spelled here rather than imported, for the same reason as `FrameKind`.
    """

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"


class MessageRef(BaseModel):
    """Which agent message an entry belongs to, within one session's transcript.

    The `frame_seq` the message opened at — ours, deterministic, and a pointer back into the log.
    Deliberately not the agent's own message id, which a great many production rows do not have.
    """

    opened_at_frame_seq: int


class _EntryBase(BaseModel):
    """What every transcript entry carries: where it sits, and where it came from."""

    index: int = Field(
        description="This entry's position in the session's transcript. `read_transcript`'s `cursor` names one."
    )
    provenance: EntryProvenance


class MessageEntry(_EntryBase):
    """One agent message, finished. `text` is absent for a message that was all thinking and tools.

    `agent_message_id` is provenance, not identity: it is what the frames called this message, and
    it is absent whenever the wire did not supply one.
    """

    kind: Literal["message"] = "message"
    message: MessageRef
    text: str | None
    agent_message_id: str | None


class ReasoningEntry(_EntryBase):
    """The agent thought, with a summary where it gave one.

    A state rather than empty prose: real messages are routinely thinking with nothing else in
    them, and a transcript that models only text renders them blank.
    """

    kind: Literal["reasoning"] = "reasoning"
    message: MessageRef
    summary: str | None


class ToolCallEntry(_EntryBase):
    """A tool was called. Its answer is a separate `tool_result` entry, joined by `call_id`."""

    kind: Literal["tool_call"] = "tool_call"
    message: MessageRef
    call_id: str
    tool_name: str
    arguments: dict[str, Any]


class ResultText(BaseModel):
    kind: Literal["text"] = "text"
    text: str


class ResultToolReferences(BaseModel):
    """The result named tools and carried no output of its own.

    A real shape rather than a defensive one: production tool results take it routinely, and a
    reader treating them as prose reads them as empty. What the call produced is in `structured`.
    """

    kind: Literal["tool_references"] = "tool_references"
    tool_names: list[str]


class ResultOpaque(BaseModel):
    """Content with no prose reading, kept verbatim. `structured` still carries the result."""

    kind: Literal["opaque"] = "opaque"
    payload: Any


type ResultContent = Annotated[ResultText | ResultToolReferences | ResultOpaque, Field(discriminator="kind")]


class ToolResultEntry(_EntryBase):
    """What a call answered: the part a transcript can print, and the part it cannot.

    **The renderable content is not the result.** `content` is prose; `structured` is the exit
    code, the patch, the MCP `structuredContent` — an open set of per-tool shapes. Both are
    carried because neither is derivable from the other, and `structured` is absent when the
    provider carried none.
    """

    kind: Literal["tool_result"] = "tool_result"
    call_id: str
    content: ResultContent
    structured: Any = Field(
        default=None, description="The call's structured output, verbatim; absent when it had none or was clipped."
    )
    clipped_bytes: int | None = Field(
        default=None,
        description="Set instead of `structured` when this entry alone overran a page's budget; its size in bytes. "
        "`provenance` names the frames to read it from.",
    )
    outcome: Outcome


class ActivityStartedEntry(_EntryBase):
    """The harness's own prose for a step in flight — the case with no tool name at all.

    `description` is whatever the harness wrote and is not a label: real ones run past 500
    characters and span lines, so a status line needs its own truncation.
    """

    kind: Literal["activity_started"] = "activity_started"
    activity_id: str
    description: str


class ActivityFinishedEntry(_EntryBase):
    """That step finished. Paired to its `activity_started` by `activity_id` and by nothing else —
    the terminal report carries no description of its own."""

    kind: Literal["activity_finished"] = "activity_finished"
    activity_id: str
    summary: str | None
    outcome: Outcome


class TurnUsage(BaseModel):
    """What one exchange cost, in terms that mean the same thing on every backend.

    **Aggregatable**: counters sum, and a counter the backend did not report is 0. Cost and
    duration are absent where it reported neither, since those cannot be invented.
    """

    input_tokens: int
    output_tokens: int
    cached_input_tokens: int
    cost_usd: float | None
    duration_ms: int | None


class TurnEndEntry(_EntryBase):
    """The exchange ended. `usage` is absent for a backend that reported none."""

    kind: Literal["turn_end"] = "turn_end"
    outcome: str = Field(description="`answered`, `aborted` or `failed`, as `list_turns` also reports it.")
    usage: TurnUsage | None


type TranscriptEntry = Annotated[
    MessageEntry
    | ReasoningEntry
    | ToolCallEntry
    | ToolResultEntry
    | ActivityStartedEntry
    | ActivityFinishedEntry
    | TurnEndEntry,
    Field(discriminator="kind"),
]


class TranscriptCursor(BaseModel):
    """A position in a session's transcript, by ordinal.

    An ordinal rather than a keyset, and safe here for the one reason an offset is ever safe: this
    order only ever grows at its *end*. The frame log is append-only and the projection is a
    deterministic left-to-right fold of it, so entry *n* is the same entry on every read. The one
    entry that can change is the last, when it belongs to a turn still in flight — which is a fact
    about that turn rather than about the cursor.

    A keyset on the frame the entry came from would not do: a console-authored entry has no
    frames at all (see `ConsoleAuthored`) and so has no position in that key.
    """

    index: int

    @classmethod
    def of(cls, entry: TranscriptEntry) -> TranscriptCursor:
        return cls(index=entry.index)


class TranscriptPage(Page[TranscriptEntry, TranscriptCursor]):
    unreadable: dict[str, int] | None = Field(
        default=None,
        description="Frame classes this release has no reading for, counted over the whole session rather than "
        "this page — so a transcript that is quietly missing something says so. Absent when there were none. "
        "`read_rollout` is how to see what they held.",
    )


class TranscriptSlice(BaseModel):
    """What the store hands back for one `read_transcript` call, before the page's byte budget.

    Up to `limit + 1` entries, like every other read here: the extra row is what tells a full page
    from the last one, and it is the row the returned cursor names.
    """

    entries: list[TranscriptEntry]
    unreadable: dict[str, int] | None


class ConversationReader(Protocol):
    """The console's session store, as this server needs it.

    A port rather than an import: the store lives in the experimental chat runtime, and a server
    registered in the stable catalog should not depend on that package's shape.

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
