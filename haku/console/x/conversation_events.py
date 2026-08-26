"""What a conversation is, once a provider's frames have been read.

The vocabulary every surface renders and every backend adapter produces. Nothing in it is
Claude-shaped: no `assistant`, no content block, no `msg_…`, no `tool_use_result`. The Claude
adapter is <claude_code/projection.py> (<README.md> § The neutral projection).

**Everything is an item, and an item is a type and three events**: started, then any number of
segments, then completed. Both stream-native harness protocols reached that decomposition
independently — Codex's `item/started` → `item/*/delta` → `item/completed` and the Responses API's
`output_item.added` → `output_text.delta` → `output_item.done` — which is the argument that it is
not our invention (<../docs/conversation_schema.md> § 1).

**Prose exists only as segments, and a completion carries none.** A backend that streams has its
adapter cut the stream into `ItemSegment`s; one that produces a final string emits exactly one
segment and then completes. So an item's text is the concatenation of its segments by construction,
a consumer replaying from a position never reprints prose it already printed, and the fold carries
no half-built string from one batch to the next.

**Tool calls are conversation, not debug**, and a lifecycle rather than records stapled to a
finished message — the room renders a call while it is still running.

**Every event says which frames it came from**, so an operator can appeal a normalization to the
raw JSON. `Provenance` is a union, not a nullable range: a console-authored event (narration, an
ownership change) has no frames and never will, and a nullable range would let a rebuild delete
them while reporting green.

**Approvals are not modelled here.** They travel over MCP to the approval queue.

The event shapes below are the projection's current neutral vocabulary; backend-specific wire
measurements belong in protocol fixtures, not in this record model.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from haku.console.chat_models import ItemType, ReasoningDisclosure, ToolOutcome, TurnOutcome

# Whatever a provider put in a field this layer passes through rather than reads. Open by nature:
# a tool's structured result is per-tool, not per-protocol.
type Json = None | bool | int | float | str | list[Json] | dict[str, Json]


@dataclass(frozen=True, slots=True)
class FrameRange:
    """The inclusive span of provider frames one event was projected from.

    A span, not a set: a message interrupted by a tool result spans the interruption too.
    """

    first_frame_seq: int
    last_frame_seq: int


@dataclass(frozen=True, slots=True)
class Authored:
    """The console said this itself, so there is no frame to appeal to.

    Distinct from a frame-derived event whose range is merely unknown: an ownership change crossed
    no wire and never will, so re-projecting frames can only preserve it.
    """


type Provenance = FrameRange | Authored


@dataclass(frozen=True, slots=True)
class CallRef:
    """An item addressed by the id the tool protocol gave it.

    A call's answer arrives frames after its ask, and the frame carrying it says only `call_id` —
    so a fold resuming from a cursor after the ask has no other handle on the item. Every harness
    protocol supplies this id precisely so the two halves can be paired, and the store's unique
    index on `(conversation_id, call_id)` is what resolves it.
    """

    call_id: str


@dataclass(frozen=True, slots=True)
class OpenRef:
    """The item of this type the fold currently has open.

    **Not a key**, and that is the point: prose belongs to the thing being written, and a backend
    writing one message at a time makes "the open one" an unambiguous answer that survives a fold
    resuming mid-item. What identity an item has is the store's `item_id`, minted when it opens;
    the fold never invents one.

    An earlier shape keyed items by the frame that opened them. That linkage did no work — nothing
    ever read the frame numbers back to find an item — while forcing an ordinal to disambiguate the
    several items one frame can open, and it leaked a session-scoped coordinate into read models
    that channels consume.
    """

    item_type: ItemType


type ItemRef = CallRef | OpenRef


@dataclass(frozen=True, slots=True)
class MessageStarted:
    """The agent began saying something. Its prose follows as segments."""

    provenance: Provenance


@dataclass(frozen=True, slots=True)
class ReasoningStarted:
    """The agent began thinking.

    Its own item, a sibling of the message rather than a part of one. Only Claude nests reasoning
    inside an assistant message, as a `thinking` block; Codex and the Responses API both make it a
    separate output item with its own id, so a shape that nested it would be one backend's promoted
    upward.
    """

    provenance: Provenance


@dataclass(frozen=True, slots=True)
class ToolCallStarted:
    """A call was asked, with the arguments it was asked with.

    **Arguments are complete or the call has not started.** Two of three backends stream them as
    partial JSON, so an adapter emits this from the frame that finishes them — "a call is being
    composed" is deliberately not expressible, and a channel learns of a call when there is
    something true to say about it.
    """

    call_id: str
    tool_name: str
    arguments: Mapping[str, Json]
    provenance: Provenance


@dataclass(frozen=True, slots=True)
class ItemSegment:
    """Prose that became visible, as an increment rather than as a whole.

    A channel renders these as they arrive, and their concatenation is the item's whole text. How
    finely a backend cuts them is the adapter's business: one segment for a backend that only ever
    produces a final string is as valid as hundreds for one that streams.

    It carries any item's prose, not a message's — a reasoning summary and a tool result's rendered
    output are the same kind of thing to every channel that shows them.
    """

    item: ItemRef
    text: str
    provenance: Provenance


@dataclass(frozen=True, slots=True)
class MessageCompleted:
    """One agent message, finished.

    Carries no text: the segments were the text, and it closes the open message rather than naming
    one. `backend_item_id` is provenance, not identity — it is what the frames called this message,
    and it is absent whenever the wire supplied none.
    """

    backend_item_id: str | None
    provenance: Provenance


@dataclass(frozen=True, slots=True)
class ReasoningCompleted:
    """The agent finished thinking, and says how much of it you may see.

    No backend we adapt returns raw chain of thought, so the distinction worth carrying is not
    summary-versus-reasoning but whether anything was disclosed at all. Without it a withheld item
    is an empty string no surface can explain.
    """

    disclosure: ReasoningDisclosure
    provenance: Provenance


@dataclass(frozen=True, slots=True)
class ToolCallCompleted:
    """What a call answered: the part a channel can show, and the part it cannot.

    **The showable part is segments**, like every other item's prose. A tool result's structure is
    the provider's — one tool's block shape on one harness — so an adapter reduces it to the text a
    transcript prints and emits that as segments before this event.

    `structured` is the exit code, the patch, the MCP `structuredContent` — an open set of per-tool
    shapes no string carries — and is None when the provider carried none. It is not derivable from
    the segments: a rendered result and a tool's own output are different answers.
    """

    item: CallRef
    structured: Json
    outcome: ToolOutcome
    provenance: Provenance


@dataclass(frozen=True, slots=True)
class TurnAnswered:
    """The agent finished the exchange."""


@dataclass(frozen=True, slots=True)
class TurnAborted:
    """Someone stopped it. It carries no reason because nothing went wrong."""


@dataclass(frozen=True, slots=True)
class TurnFailed:
    """The runtime could not finish, and why.

    The reason is required, so that a failure cannot be recorded without saying what failed. It is
    bounded prose for an operator to read, in whatever words the provider used; nothing dispatches
    on it, which is why there is no category beside it.
    """

    reason: str


type TurnEnd = TurnAnswered | TurnAborted | TurnFailed


@dataclass(frozen=True, slots=True)
class TurnCompleted:
    """The exchange ended, and how.

    One event rather than three, because every consumer that cares which frame ended a turn cares
    about all three ways it can end; `end` is what they dispatch on.
    """

    end: TurnEnd
    provenance: Provenance


def outcome_of(end: TurnEnd) -> TurnOutcome:
    """`end` in the console's own durable vocabulary, which keeps no reason."""
    match end:
        case TurnAnswered():
            return TurnOutcome.ANSWERED
        case TurnAborted():
            return TurnOutcome.ABORTED
        case TurnFailed():
            return TurnOutcome.FAILED


type ItemStarted = MessageStarted | ReasoningStarted | ToolCallStarted
type ItemCompleted = MessageCompleted | ReasoningCompleted | ToolCallCompleted
type ConversationEvent = ItemStarted | ItemSegment | ItemCompleted | TurnCompleted


@dataclass(frozen=True, slots=True)
class Projection:
    """What a stretch of a backend's frames meant, plus what it held that this release cannot read.

    `unprojected` counts by whatever the adapter calls a frame class — for the Claude adapter,
    `tool_progress`, `system/vcs_state_changed`, `user/text` — so the default branch stays
    observable without costing an event per frame. An adapter's *deliberately* ignored classes are
    not in it: the actionable signal is "the backend is sending something we do not map".
    """

    events: tuple[ConversationEvent, ...]
    unprojected: Mapping[str, int]
