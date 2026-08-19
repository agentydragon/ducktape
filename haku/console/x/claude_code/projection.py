"""Claude CLI frames into neutral conversation events.

A **reducer**: `project(state, frames)` returns the state after those frames and what they
produced. The state is neutral (`ProjectionState`, in <../conversation_events.py>), the frames are
Claude's — generic over its state, not over its wire. It is resumable from a cursor: a position
in the frame log is the whole of what a fold needs to carry on from.

That is what makes live and recovery one code path: steady state projects each frame as it lands,
adoption projects from a cursor that is behind, and the two agree because one batch and any split
of batches produce the same projection. `test_projection.py` asserts it over every split.

Determinism is what the design rests on: re-projecting a stored session reproduces its rows
exactly, so drift is detectable by comparison and a projection bug is repairable by fixing the fold
rather than baked into a row forever.

**Written against what the wire does, not what it documents.** Every rule below that looks
defensive is a finding from <../../debug/frame_shape_census.md>, where the measurements live — a
share of production frames is a dated observation and belongs in a dated document.

| What the wire does                                              | What this does with it                                                                                                                    |
| ---------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------ |
| One block per `assistant` frame, so a message spans several      | A message is the run of frames sharing `message.id`, closed by a *different* id, a `result`, or the caller saying the stream ended — never by a clock or a batch boundary |
| `stop_reason` is never set                                       | Nothing reads it; there is no "the provider said it was done" branch to be wrong                                                           |
| A `user` frame can land between two frames of one message        | A `user` frame never closes a message. Its `FrameRange` spans the interruption, which is what a range means                                |
| `content` is usually prose, sometimes tool names and no payload  | `content` is rendered to text whatever shape it arrived in, and the real output rides beside it as `structured` (`tool_use_result`, many per-tool shapes) |
| `is_error` is often absent, and `result.is_error` is never true  | `ToolOutcome.UNKNOWN` where it is absent, and a turn's outcome comes from `subtype` — `is_error` is read nowhere                           |
| Most of the wire is `system`, and most of that is one constant   | `_IGNORED_KINDS` / `_IGNORED_SYSTEM_SUBTYPES` are frozenset lookups that return before anything is allocated                               |
| Frame classes and `system` subtypes exist that `protocol.md` omits | The default branch counts into `Projection.unprojected` — neither a crash nor a silent drop                                                |
| `command_lifecycle` is not a clean triple                        | It is not read at all: turn boundaries come from `result`, so no sequence assumption exists to be violated                                 |

Where an `ItemSegment` is cut is the only thing the wire and the stored log disagree about — see
`DeltaSource`.

**`result.result` is not projected as prose.** It repeats the final message, so minting one from it
would double every answer. A turn that produced no message item said nothing, which is a fact worth
being able to see.

**A `thinking` block is a whole reasoning item**, opened, segmented and completed by the one frame
that carries it — Claude nests it in an assistant message, and unnesting it here is what keeps that
one backend's shape out of the record. `redacted_thinking` completes with no segments at all, which
is the case `ReasoningDisclosure.WITHHELD` exists to render.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from haku.console.chat_models import ItemType, ReasoningDisclosure, ToolOutcome, TurnOutcome
from haku.console.x.claude_code import frames
from haku.console.x.conversation_events import (
    CallRef,
    ConversationEvent,
    FrameRange,
    ItemSegment,
    Json,
    MessageCompleted,
    MessageStarted,
    OpenItem,
    OpenRef,
    Projection,
    ProjectionState,
    ReasoningCompleted,
    ReasoningStarted,
    ToolCallCompleted,
    ToolCallStarted,
    TurnCompleted,
)

# Frame classes that say nothing about the conversation, listed rather than discovered so that a
# class the CLI adds lands in the default branch instead of here:
#
# - `command_lifecycle` — which prompt the CLI is working on. Not a clean triple (no `cancelled`
#   ever, commands that start without queueing, commands that never complete, and `command_uuid`s
#   matching no prompt the console sent), and nothing here needs it to be: a turn ends at `result`.
# - `control_request` / `control_response` — the other channel entirely.
# - `rate_limit_event` — the account's state, not the conversation's.
#
# `stream_event` is not here: it is read or skipped by `DeltaSource`, which is a decision and not
# an omission.
_IGNORED_KINDS = frozenset({"command_lifecycle", "control_request", "control_response", "rate_limit_event"})

# The bulk of the log by volume, and none of it conversation: `thinking_tokens` is budget
# accounting and `status` is a heartbeat wearing a discriminator (one distinct value across the
# whole corpus). `init` is session identity, which is a session event rather than a conversation
# one.
_IGNORED_SYSTEM_SUBTYPES = frozenset({"thinking_tokens", "status", "init"})


class DeltaSource(StrEnum):
    """Which frames a projection cuts its `ItemSegment`s from.

    Granularity, not content: an item's whole text is the same prose whichever is chosen, and the
    vocabulary already says how finely a backend cuts an increment is the adapter's business.

    `COMPLETED_BLOCKS` is the only honest reading of a *stored* log: most sessions emit no
    `stream_event` at all, those that do are mostly `input_json_delta` (tool arguments, not prose),
    they carry no identity so `haku/cli_protocol/frame_identity.py` refuses to dedupe them, and a
    log truncated mid-block would re-project to different text than the completed block that
    follows it — which is the determinism the whole design rests on. `STREAM_EVENTS` is what a
    consumer holding the live wire drives: it takes the increments as they arrive and skips the
    completed block's own text, which those increments already delivered.
    """

    COMPLETED_BLOCKS = "completed_blocks"
    STREAM_EVENTS = "stream_events"


@dataclass(frozen=True, slots=True)
class RecordedFrame:
    """One row of the frame log: a CLI protocol frame and where it sits in the session.

    `payload` is the wire verbatim, hence `.get` and type guards throughout — an unseen frame is a
    new CLI feature, not a bug here. A row the CLI did not author — the bridge's `setup_output` —
    does not belong here, and `payload["type"]` is what says so.
    """

    frame_seq: int
    payload: dict[str, Any]


def project(
    state: ProjectionState, frames: Iterable[RecordedFrame], *, delta_source: DeltaSource = DeltaSource.COMPLETED_BLOCKS
) -> tuple[ProjectionState, Projection]:
    """Fold frames into the state: the state after them, and what they produced.

    Pure and order-dependent — the events are a function of the sequence, not of any one frame —
    and the state holds everything that order-dependence needs, which is why a batch boundary is
    not an event. Raises `ValueError` on a payload with no `type`: that is a caller handing it a
    row the CLI never sent, not anything the wire can do.
    """
    projector = _Projector(delta_source=delta_source, open_message=state.open_message)
    for frame in frames:
        projector.fold(frame)
    return ProjectionState(open_message=projector.open_message), projector.projected()


def finish(state: ProjectionState) -> Projection:
    """What ending the stream produces: an open message completed, or nothing at all.

    A message still open when the frames run out is one whose turn died mid-answer, which real
    sessions do (<../../debug/frame_shape_census.md>); it completes with what it had rather than
    being lost. Only a caller that knows no more frames are coming may say this — for one reading a
    live wire, the next frame is the continuation.

    No `DeltaSource`, and that is the shape saying something true: ending a stream reads no frame,
    so how finely one would be cut cannot arise.
    """
    if (open_message := state.open_message) is None:
        return Projection(events=(), unprojected=MappingProxyType({}))
    return Projection(events=(_completed(open_message),), unprojected=MappingProxyType({}))


def undelivered(text: str, delivered: str) -> str:
    """The part of a completed block nobody has been shown yet.

    A block's deltas deliver its prose as it is written and the completed block repeats all of it,
    so emitting the block whole would have every consumer render the answer twice. What has been
    delivered is a **prefix of this block** only while one process folds the whole message: a fold
    resuming an item another process left open inherits that item's prose entire, which may hold
    blocks finished before the one now arriving. So the overlap is what is subtracted — the longest
    prefix of the block that the delivered prose ends with — which is the full watermark in the
    first case, nothing in the second, and the whole block wherever a backend streams no deltas at
    all.
    """
    overlap = min(len(text), len(delivered))
    while overlap and not delivered.endswith(text[:overlap]):
        overlap -= 1
    return text[overlap:]


def _completed(open_message: OpenItem) -> MessageCompleted:
    """The message's close. It carries no prose: the segments already delivered every word.

    The frame span is the item's provenance — where it began and where it ended on the wire — which
    is the one thing those numbers are for.
    """
    return MessageCompleted(
        backend_item_id=open_message.backend_item_id,
        provenance=FrameRange(open_message.opened_at_frame_seq, open_message.last_frame_seq),
    )


def project_log(
    frames: Iterable[RecordedFrame], *, delta_source: DeltaSource = DeltaSource.COMPLETED_BLOCKS
) -> Projection:
    """A whole session's frames, with nothing after them — the reader's shape of the reducer."""
    state, projected = project(ProjectionState(), frames, delta_source=delta_source)
    return projected.then(finish(state))


@dataclass(slots=True)
class _Projector:
    delta_source: DeltaSource
    open_message: OpenItem | None
    events: list[ConversationEvent] = field(default_factory=list)
    unprojected: dict[str, int] = field(default_factory=dict)

    def projected(self) -> Projection:
        return Projection(events=tuple(self.events), unprojected=MappingProxyType(dict(self.unprojected)))

    def fold(self, frame: RecordedFrame) -> None:
        kind = frames.frame_kind(frame.payload)
        if kind == frames.DELTA_FRAME_KIND:
            if self.delta_source is DeltaSource.STREAM_EVENTS:
                self._stream_delta(frame)
            return
        if kind in _IGNORED_KINDS:
            return
        match kind:
            case frames.ASSISTANT_FRAME_KIND:
                self._assistant(frame)
            case frames.PROMPT_FRAME_KIND:
                self._user(frame)
            case frames.RESULT_FRAME_KIND:
                self._result(frame)
            case "system":
                self._system(frame)
            case _:
                self._unprojected(kind)

    def close_message(self) -> None:
        """End the open message, if there is one. Called where the census says a message ends — a
        different `message.id`, a `result`, a caller declaring the stream over — and nowhere else.
        Running out of frames is not one of those: `project` leaves it in the state."""
        if (open_message := self.open_message) is None:
            return
        self.open_message = None
        self.events.append(_completed(open_message))

    def _stream_delta(self, frame: RecordedFrame) -> None:
        """One increment of an answer still being written, for a consumer holding the live wire.

        **It attaches to the open message rather than keying itself.** A delta carries no
        `message.id` to group by, and prose belongs to an item — so a delta arriving with nothing
        open opens a message at its own frame, and every delta after it joins that one. Keying each
        delta separately would mint an item per increment, which is the shape this vocabulary
        exists to rule out. A CLI writes one answer at a time, which is what makes "the open one"
        unambiguous.
        """
        event = frame.payload.get("event")
        if not isinstance(event, dict) or not (text := frames.text_delta(event)):
            return
        if self.open_message is None:
            self.open_message = OpenItem(
                opened_at_frame_seq=frame.frame_seq, last_frame_seq=frame.frame_seq, backend_item_id=None
            )
            self.events.append(MessageStarted(provenance=FrameRange(frame.frame_seq, frame.frame_seq)))
        self.open_message = replace(
            self.open_message, last_frame_seq=frame.frame_seq, delivered=self.open_message.delivered + text
        )
        self.events.append(
            ItemSegment(
                item=OpenRef(item_type=ItemType.MESSAGE),
                text=text,
                provenance=FrameRange(frame.frame_seq, frame.frame_seq),
            )
        )

    def _assistant(self, frame: RecordedFrame) -> None:
        where = FrameRange(frame.frame_seq, frame.frame_seq)
        # **A different `message.id` ends the message before it, whatever this frame carries.** The
        # run is defined by the id and not by which block types happen to be in the frame that
        # breaks it — so a frame of pure thinking closes the previous answer here rather than
        # leaving it open until the next frame with prose in it, which would order a transcript by
        # something other than what happened.
        if (
            (open_message := self.open_message) is not None
            and (breaking := frames.agent_message_id(frame.payload)) is not None
            and open_message.backend_item_id not in (None, breaking)
        ):
            self.close_message()
        for block in frames.content_blocks(frame.payload):
            match block.get("type"):
                case "text" if isinstance(text := block.get("text"), str):
                    message = self._message_for(frame)
                    if remainder := undelivered(text, message.delivered):
                        self.events.append(
                            ItemSegment(item=OpenRef(item_type=ItemType.MESSAGE), text=remainder, provenance=where)
                        )
                    # The block is finished, so the next one starts undelivered.
                    self.open_message = replace(message, delivered="")
                case "thinking":
                    # Opened, spoken and closed by this one frame, so nothing has to name it: it is
                    # the open reasoning item for exactly as long as these three events take.
                    summary = block.get("thinking")
                    self.events.append(ReasoningStarted(provenance=where))
                    if isinstance(summary, str) and summary:
                        self.events.append(
                            ItemSegment(item=OpenRef(item_type=ItemType.REASONING), text=summary, provenance=where)
                        )
                    self.events.append(ReasoningCompleted(disclosure=ReasoningDisclosure.SUMMARY, provenance=where))
                case "redacted_thinking":
                    # The model thought and none of it is available. Rendered as an item with no
                    # segments, which is the one thing an empty string could not say.
                    self.events.append(ReasoningStarted(provenance=where))
                    self.events.append(ReasoningCompleted(disclosure=ReasoningDisclosure.WITHHELD, provenance=where))
                case "tool_use" if (
                    isinstance(call_id := block.get("id"), str)
                    and isinstance(name := block.get("name"), str)
                    and isinstance(arguments := block.get("input"), dict)
                ):
                    self.events.append(
                        ToolCallStarted(call_id=call_id, tool_name=name, arguments=arguments, provenance=where)
                    )
                case block_type:
                    self._unprojected(f"{frames.ASSISTANT_FRAME_KIND}/{block_type}")

    def _message_for(self, frame: RecordedFrame) -> OpenItem:
        """The message this frame continues, or a new one.

        The run is defined by `message.id` and closed by a different one — not by the next
        non-`assistant` frame, which would split a real message containing a tool result and
        attribute its second call to a message that does not exist. A frame with no id cannot be
        grouped, so it is its own message; the wire supplies one essentially always, the exceptions
        being the console's own reconstructions.

        **A message being streamed into absorbs the frame that completes it, id or no id.** Deltas
        carry no `message.id` to group by, so a message they opened has none until a completed block
        gives it one — and treating that as "a different id" would close the message the operator has
        been watching arrive and open a second one for the same prose, empty under `STREAM_EVENTS`
        since the deltas already delivered every word of it. Mid-stream is the whole of that
        exception: once the block has been completed the item's prose is delivered again from zero,
        so the next unkeyable frame is its own message as ever.
        """
        backend_item_id = frames.agent_message_id(frame.payload)
        if (open_message := self.open_message) is not None and (
            open_message.backend_item_id == backend_item_id
            if open_message.backend_item_id is not None
            else bool(open_message.delivered)
        ):
            continued = replace(open_message, last_frame_seq=frame.frame_seq, backend_item_id=backend_item_id)
            self.open_message = continued
            return continued
        self.close_message()
        started = OpenItem(
            opened_at_frame_seq=frame.frame_seq, last_frame_seq=frame.frame_seq, backend_item_id=backend_item_id
        )
        self.open_message = started
        self.events.append(MessageStarted(provenance=FrameRange(frame.frame_seq, frame.frame_seq)))
        return started

    def _user(self, frame: RecordedFrame) -> None:
        """A tool result coming back, or the console's own prompt going out.

        The content type gives the direction, without exception in the corpus: an outbound prompt
        carries a string, an inbound frame a list. An outbound prompt projects to nothing — it is
        the console's own text, which the console already holds.
        """
        message = frame.payload.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, str):
            return
        if not isinstance(content, list):
            self._unprojected(frames.PROMPT_FRAME_KIND)
            return
        # Top-level and undocumented, and the channel the tool's real output arrives on. One per
        # frame — as is the `tool_result` block it belongs to, in every production result seen.
        structured: Json = frame.payload.get("tool_use_result")
        where = FrameRange(frame.frame_seq, frame.frame_seq)
        for block in content:
            match block.get("type") if isinstance(block, dict) else None:
                case "tool_result" if isinstance(call_id := block.get("tool_use_id"), str):
                    # Addressed by the call id: the ask was frames ago, and a fold resuming from a
                    # cursor after it has no position to name.
                    answered = CallRef(call_id=call_id)
                    if rendered := _result_content(block.get("content")):
                        self.events.append(ItemSegment(item=answered, text=rendered, provenance=where))
                    self.events.append(
                        ToolCallCompleted(
                            item=answered,
                            structured=structured,
                            outcome=_result_outcome(block.get("is_error")),
                            provenance=where,
                        )
                    )
                case block_type:
                    self._unprojected(f"{frames.PROMPT_FRAME_KIND}/{block_type}")

    def _result(self, frame: RecordedFrame) -> None:
        self.close_message()
        # From `subtype` alone. `is_error` is false on every production result, including those
        # from sessions the console records as failed, so reading it would report every turn as
        # fine and one field disagreeing with another as a contradiction.
        subtype = frame.payload.get("subtype")
        self.events.append(
            TurnCompleted(
                outcome=TurnOutcome.ANSWERED if subtype == "success" else TurnOutcome.FAILED,
                provenance=FrameRange(frame.frame_seq, frame.frame_seq),
            )
        )

    def _system(self, frame: RecordedFrame) -> None:
        subtype = frame.payload.get("subtype")
        if subtype not in _IGNORED_SYSTEM_SUBTYPES:
            self._unprojected(f"system/{subtype}")

    def _unprojected(self, key: str) -> None:
        self.unprojected[key] = self.unprojected.get(key, 0) + 1


def _result_content(content: Any) -> str:
    """The renderable half of a tool result, as the text a transcript prints.

    Prose in the two shapes that carry prose — a bare string, and a list of `text` blocks, which is
    what an MCP tool's result arrives as. Everything else is rendered as its JSON rather than given
    an arm of its own: a `tool_result`'s block set is the provider's to extend, and the one other
    shape production sends is a list of `tool_reference` blocks, which is one built-in search's
    result on one harness. What the call actually produced is in `structured` either way.
    """
    if isinstance(content, str):
        return content
    if (
        isinstance(content, list)
        and content
        and all(
            isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str)
            for block in content
        )
    ):
        return "".join(block["text"] for block in content)
    return json.dumps(content)


def _result_outcome(is_error: Any) -> ToolOutcome:
    match is_error:
        case True:
            return ToolOutcome.FAILED
        case False:
            return ToolOutcome.SUCCEEDED
        case _:
            # Routinely absent rather than false, so `"is_error" in block` tests nothing.
            return ToolOutcome.UNKNOWN
