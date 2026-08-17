"""What a conversation is, once a provider's frames have been read.

The vocabulary every surface renders and every backend adapter produces. Nothing in it is
Claude-shaped: no `assistant`, no content block, no `msg_…`, no `tool_use_result`. The Claude
adapter is <claude_code/projection.py> (<README.md> § The neutral projection).

**Tool calls are conversation, not debug**, and a lifecycle rather than records stapled to a
finished message — the room renders a call while it is still running.

**Every event says which frames it came from**, so an operator can appeal a normalization to the
raw JSON. `Provenance` is a union, not a nullable range: a console-authored event (narration, an
ownership change) has no frames and never will, and a nullable range would let a rebuild delete
them while reporting green.

**Approvals are not modelled here.** They travel over MCP to the approval queue.

**`ProjectionState` is here rather than beside the adapter**: a second backend adapter has to be
able to produce one.

Every shape claim below was read off production frames; the measurements are in
<../debug/frame_shape_census.md> § What will break a naive fold.
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
class MessageKey:
    """Which agent message an event belongs to, within one session's projection.

    The `frame_seq` it opened at — ours, deterministic, a pointer back into the log. Not the
    agent's own message id, which many production rows lack; that rides on `MessageCompleted` as
    provenance, where its absence costs nothing.
    """

    opened_at_frame_seq: int


@dataclass(frozen=True, slots=True)
class TextDelta:
    """Prose that became visible, as an increment rather than as a whole.

    A channel renders these as they arrive; `MessageCompleted.text` is the same prose joined. How
    finely a backend cuts them is the adapter's business.
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

    A state rather than empty prose: many real messages are thinking and nothing else, and a
    transcript modelling only text renders them blank.
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
    routinely absent, and collapsing that into `SUCCEEDED` reports every unanswerable case as fine.
    """

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ToolCallCompleted:
    """What a call answered: the part a channel can show, and the part it cannot.

    **`content` is the result rendered, not the result.** A tool result's structure is the
    provider's — one tool's block shape on one harness — so an adapter reduces it to the text a
    transcript prints, and a channel branching on a variant would be branching on a shape only one
    backend produces.

    `structured` is the exit code, the patch, the MCP `structuredContent` — an open set of per-tool
    shapes no string carries — and is None when the provider carried none. It is not derivable from
    `content`: a rendered result and a tool's own output are different answers.
    """

    call_id: str
    content: str
    structured: Json
    outcome: Outcome
    provenance: Provenance


@dataclass(frozen=True, slots=True)
class TurnCompleted:
    """The exchange ended. `TurnOutcome` is the console's own durable vocabulary."""

    outcome: TurnOutcome
    provenance: Provenance


type ConversationEvent = TextDelta | MessageCompleted | Reasoning | ToolCallStarted | ToolCallCompleted | TurnCompleted


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

    def then(self, later: Projection) -> Projection:
        """This stretch of frames followed by the next one, as a single projection.

        Frames read in one batch and the same frames read in any split, combined this way, are
        equal. Counts sum because `unprojected` tallies frames read, not a set of what exists.
        """
        return Projection(
            events=self.events + later.events,
            unprojected=MappingProxyType(dict(Counter(self.unprojected) + Counter(later.unprojected))),
        )


@dataclass(frozen=True, slots=True)
class OpenMessage:
    """An agent message the fold has seen the start of and not the end of.

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

    A value, not a session: it says only what is mid-flight when a batch ends, so one state serves
    both a live consumer between frames and a cursor-driven one reloading. The default is a stream
    nothing has been read from yet.

    Only an open message is in flight. Everything else the fold decides — a tool call's identity,
    an activity's pairing, a turn's outcome — is settled by the frame that produced it.
    """

    open_message: OpenMessage | None = None
