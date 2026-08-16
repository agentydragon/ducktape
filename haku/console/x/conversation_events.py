"""What a conversation is, once a provider's frames have been read.

The vocabulary every surface renders and every backend adapter produces. Nothing in it is
Claude-shaped: no `assistant`, no content block, no `msg_…`, no `tool_use_result`. A Claude
adapter (<claude_code/projection.py>) is the only producer today, and the point of the layer is that a
second backend is a second adapter rather than a second interpretation of what a message is
(<../../plans/chat_runtime_projection.md> § stage 4, which counts the four interpreters this
replaces).

Three things this vocabulary is deliberate about.

**Tool calls are conversation, not debug.** The room already renders calls in progress, so a
neutral source has to carry them — and as a lifecycle (`ToolCallStarted` → `ToolCallCompleted`)
rather than as finished records stapled to a completed message, because the display exists while
the call is still running and the message it belongs to is not finished yet.

**Every event says which frames it came from**, so an operator can appeal a normalization to the
raw JSON behind it. `Provenance` is a union rather than a nullable range: a frame-derived event
has a `FrameRange`, and a console-authored one — bootstrap narration, the replica owning a
session changing hands — has no frames and never will. Re-projecting a session must *preserve*
those rather than re-derive them, and a nullable range would let a rebuild delete them while
reporting green.

**Nothing here models approvals.** They travel over MCP to the console's approval queue and never
appear on this channel.

**The fold's own state is here too.** `ProjectionState` is what an adapter carries from one batch
of frames to the next, and it is stated in this vocabulary rather than in any provider's — a
second backend adapter has to be able to produce one. That is what makes "project each frame as
it lands" and "project from the stored cursor, which happens to be behind" the same code path
(<../../plans/chat_runtime_projection.md> § The shape).

`Outcome.UNKNOWN` is the other load-bearing choice, and it is a measured one: `is_error` is
*absent* rather than false on most real tool results, so "did this go wrong" has three answers and
a two-valued type would have to guess one of them. That and every other shape claim below was read
off production frames; the measurements themselves live in
<../debug/frame_shape_census.md> § What will break a naive fold, which is a dated document, where
a share of production frames keeps its date.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType

from haku.console.chat_models import TurnOutcome

# Whatever a provider put in a field this layer passes through rather than reads. Open by nature:
# a tool's structured result is per-tool, not per-protocol.
type Json = None | bool | int | float | str | list[Json] | dict[str, Json]


@dataclass(frozen=True, slots=True)
class FrameRange:
    """The inclusive span of provider frames one event was projected from.

    Inclusive of everything between the ends, which is not the same as "these frames and no
    others": a message whose frames are interrupted by a tool result spans the interruption too,
    and that is the honest reading of a range rather than a defect in it.
    """

    first_frame_seq: int
    last_frame_seq: int


@dataclass(frozen=True, slots=True)
class Authored:
    """The console said this itself, so there is no frame to appeal to.

    Distinct in kind from a frame-derived event whose range happens to be unknown: an ownership
    change crossed no wire and never will, so re-projecting frames can only preserve it.
    """


type Provenance = FrameRange | Authored


@dataclass(frozen=True, slots=True)
class MessageKey:
    """Which agent message an event belongs to, within one session's projection.

    The `frame_seq` the message opened at — ours, deterministic, and a pointer back into the log.
    Deliberately not the agent's own message id, which a great many production rows do not have;
    that id rides on `MessageCompleted` as provenance, where its absence costs nothing.
    """

    opened_at_frame_seq: int


@dataclass(frozen=True, slots=True)
class TextDelta:
    """Prose that became visible, as an increment rather than as a whole.

    A channel renders these as they arrive; `MessageCompleted.text` is the same prose joined, for
    the durable transcript row. How finely a backend cuts them is the adapter's business.
    """

    message: MessageKey
    text: str
    provenance: Provenance


@dataclass(frozen=True, slots=True)
class MessageCompleted:
    """One agent message, finished. `text` is None for a message that was all thinking and tools.

    `agent_message_id` is provenance, not identity: it is what the frames called this message, and
    it is absent whenever the wire did not supply one.
    """

    message: MessageKey
    text: str | None
    agent_message_id: str | None
    provenance: Provenance


@dataclass(frozen=True, slots=True)
class Reasoning:
    """The agent thought, with a summary where it gave one.

    A state rather than empty prose: a substantial share of real messages are thinking with
    nothing else in them, and a transcript that models only text renders them blank.
    """

    message: MessageKey
    summary: str | None
    provenance: Provenance


@dataclass(frozen=True, slots=True)
class ToolCallStarted:
    message: MessageKey
    call_id: str
    tool_name: str
    arguments: Mapping[str, Json]
    provenance: Provenance


class Outcome(StrEnum):
    """How a step ended, where "cannot tell" is a first-class answer rather than a default.

    `UNKNOWN` is the common case, not the corner: the field a provider would report failure in is
    routinely absent, and collapsing that into `SUCCEEDED` reports every unanswerable case as
    fine.
    """

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class TextContent:
    text: str


@dataclass(frozen=True, slots=True)
class ToolReferences:
    """The result named tools and carried no output of its own.

    A real shape rather than a defensive one: production tool results take it routinely, and a
    renderer that reads them as prose renders them empty. What the call actually produced is in
    `ToolCallCompleted.structured`.
    """

    tool_names: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class OpaqueContent:
    """Content this projection has no prose reading for, kept verbatim.

    The branch exists because the block set is the provider's to extend, not because anything has
    been seen here. `structured` still carries the result.
    """

    payload: Json


type ToolResultContent = TextContent | ToolReferences | OpaqueContent


@dataclass(frozen=True, slots=True)
class ToolCallCompleted:
    """What a call answered: the part a channel can show, and the part it cannot.

    **The renderable content is not the result.** `content` is what a transcript prints;
    `structured` is the exit code, the patch, the MCP `structuredContent` — an open set of
    per-tool shapes that a `str | list[Block]` model drops in silence. Both are carried because
    neither is derivable from the other.

    `structured` is None when the provider carried no structured result at all.
    """

    call_id: str
    content: ToolResultContent
    structured: Json
    outcome: Outcome
    provenance: Provenance


@dataclass(frozen=True, slots=True)
class ActivityStarted:
    """The harness's own prose for a step in flight — the case with no tool name at all.

    `description` is whatever the harness wrote and is not a label: real ones run past 500
    characters and span lines, so a status line needs its own truncation.
    """

    activity_id: str
    description: str
    provenance: Provenance


@dataclass(frozen=True, slots=True)
class ActivityCompleted:
    """That step finished. Paired to `ActivityStarted` by `activity_id` and by nothing else —
    the terminal report carries no description of its own."""

    activity_id: str
    summary: str | None
    outcome: Outcome
    provenance: Provenance


@dataclass(frozen=True, slots=True)
class Usage:
    """What one exchange cost, in terms that mean the same thing on every backend.

    **Aggregatable**, because a neutral turn may one day span several provider invocations:
    counters sum. A counter the backend did not report is 0 and contributes nothing to a sum;
    cost and duration are None where it reported neither, since those cannot be invented.
    """

    input_tokens: int
    output_tokens: int
    cached_input_tokens: int
    cost_usd: float | None
    duration_ms: int | None


@dataclass(frozen=True, slots=True)
class TurnCompleted:
    """The exchange ended.

    `TurnOutcome` is the console's existing durable vocabulary rather than a second enum meaning
    the same thing. `usage` is None for a backend that reported none.
    """

    outcome: TurnOutcome
    usage: Usage | None
    provenance: Provenance


type ConversationEvent = (
    TextDelta
    | MessageCompleted
    | Reasoning
    | ToolCallStarted
    | ToolCallCompleted
    | ActivityStarted
    | ActivityCompleted
    | TurnCompleted
)


@dataclass(frozen=True, slots=True)
class Projection:
    """What a stretch of a backend's frames meant, plus what it held that this release cannot read.

    Here rather than beside the Claude adapter because neither half mentions a backend: the events
    are this vocabulary, and an unreadable frame is a fact about the reader. A second adapter
    returns this same type, which is what lets a surface consume one without knowing which produced
    it.

    `unprojected` counts by whatever the adapter calls a frame class — for the Claude adapter,
    `tool_progress`, `system/vcs_state_changed`, `user/text`. It is how the default branch stays
    observable without costing an event per frame. An adapter's *deliberately* ignored classes are
    not in it: the actionable signal is "the backend is sending something we do not map", not "the
    heartbeat beat again".
    """

    events: tuple[ConversationEvent, ...]
    unprojected: Mapping[str, int]

    def then(self, later: Projection) -> Projection:
        """This stretch of frames followed by the next one, as a single projection.

        The anti-drift invariant written as an operation: a projection of frames read in one
        batch and the same frames read in any split of batches, combined this way, are equal.
        Events concatenate because the stream is ordered; counts sum because `unprojected` is a
        tally over the frames read, not a set of what exists.
        """
        return Projection(
            events=self.events + later.events,
            unprojected=MappingProxyType(dict(Counter(self.unprojected) + Counter(later.unprojected))),
        )


@dataclass(frozen=True, slots=True)
class OpenMessage:
    """An agent message the fold has seen the start of and not the end of.

    Every field is this vocabulary's own — the key the events already carry, the far end of the
    range they will be given, the agent's optional id, and the prose so far — so an adapter for a
    second backend produces one without borrowing anything Claude-shaped.

    `texts` is the message's deltas in order rather than the joined prose: the vocabulary's
    contract is that they concatenate to exactly the `text` its `MessageCompleted` carries, and
    keeping them apart is what lets the join happen once, where that event is minted.
    """

    key: MessageKey
    agent_message_id: str | None
    last_frame_seq: int
    texts: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ProjectionState:
    """What a fold carries from one batch of frames to the next.

    A value, not a session: it says only what is mid-flight when a batch ends, which is why the
    same state is what a live consumer holds between frames and what a cursor-driven one would
    reload. The default is a stream nothing has been read from yet.

    Only an open message is in flight. Everything else the fold decides — a tool call's identity,
    an activity's pairing, a turn's outcome — is settled by the frame that produced it, so it is
    an effect and never a carry.
    """

    open_message: OpenMessage | None = None
