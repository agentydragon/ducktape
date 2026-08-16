"""Claude CLI frames into neutral conversation events.

A **reducer**: `project(state, frames)` returns the state after those frames and what they
produced. The state is neutral (`ProjectionState`, in <../conversation_events.py>), the frames are
Claude's — generic over its state, not over its wire
(<../../../plans/chat_runtime_projection.md> § The shape).

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
| `content` is usually prose, sometimes tool names and no payload  | `content` is a variant, and the real output rides beside it as `structured` (`tool_use_result`, many per-tool shapes)                      |
| `is_error` is often absent, and `result.is_error` is never true  | `Outcome.UNKNOWN` where it is absent, and a turn's outcome comes from `subtype` — `is_error` is read nowhere                               |
| Most of the wire is `system`, and most of that is one constant   | `_IGNORED_KINDS` / `_IGNORED_SYSTEM_SUBTYPES` are frozenset lookups that return before anything is allocated                               |
| Frame classes and `system` subtypes exist that `protocol.md` omits | The default branch counts into `Projection.unprojected` — neither a crash nor a silent drop                                                |
| `command_lifecycle` is not a clean triple                        | It is not read at all: turn boundaries come from `result`, so no sequence assumption exists to be violated                                 |

Where a `TextDelta` is cut is the only thing the wire and the stored log disagree about — see
`DeltaSource`.

**`result.result` is not projected as prose.** It repeats the final message, so minting one from it
would double every answer. A turn that produced no `MessageCompleted` said nothing, which is a fact
worth being able to see.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from haku.console.chat_models import TurnOutcome
from haku.console.x.claude_code import frames
from haku.console.x.conversation_events import (
    ActivityCompleted,
    ActivityStarted,
    ConversationEvent,
    FrameRange,
    Json,
    MessageCompleted,
    MessageKey,
    OpaqueContent,
    OpenMessage,
    Outcome,
    Projection,
    ProjectionState,
    Reasoning,
    TextContent,
    TextDelta,
    ToolCallCompleted,
    ToolCallStarted,
    ToolReferences,
    ToolResultContent,
    TurnCompleted,
    Usage,
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
    """Which frames a projection cuts its `TextDelta`s from.

    Granularity, not content: `MessageCompleted.text` is the same prose whichever is chosen, and
    the vocabulary already says how finely a backend cuts an increment is the adapter's business.

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
    new CLI feature, not a bug here. A row the CLI did not author (the bridge's `setup_output`, the
    console's own `partial`) does not belong here, and `payload["type"]` is what says so.
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


def _completed(open_message: OpenMessage) -> MessageCompleted:
    return MessageCompleted(
        message=open_message.key,
        # Joined bare, because the deltas are increments of one answer rather than paragraphs of it.
        text="".join(open_message.texts) or None,
        agent_message_id=open_message.agent_message_id,
        provenance=FrameRange(open_message.key.opened_at_frame_seq, open_message.last_frame_seq),
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
    open_message: OpenMessage | None
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

        **It opens no message.** A delta carries no `message.id` to group by; the completed block
        that follows says which message the prose belonged to. Its `MessageKey` is therefore the
        delta's own frame — enough for a consumer tracking one open message at a time, which every
        live one does, since a CLI writes one answer at a time.
        """
        event = frame.payload.get("event")
        if not isinstance(event, dict) or not (text := frames.text_delta(event)):
            return
        self.events.append(
            TextDelta(
                message=MessageKey(opened_at_frame_seq=frame.frame_seq),
                text=text,
                provenance=FrameRange(frame.frame_seq, frame.frame_seq),
            )
        )

    def _assistant(self, frame: RecordedFrame) -> None:
        message = self._message_for(frame)
        where = FrameRange(frame.frame_seq, frame.frame_seq)
        for block in frames.content_blocks(frame.payload):
            match block.get("type"):
                case "text" if isinstance(text := block.get("text"), str):
                    message = replace(message, texts=(*message.texts, text))
                    self.open_message = message
                    # Under `STREAM_EVENTS` the deltas already delivered this prose; emitting it
                    # again as a whole would have a consumer render the answer twice.
                    if self.delta_source is DeltaSource.COMPLETED_BLOCKS:
                        self.events.append(TextDelta(message=message.key, text=text, provenance=where))
                case "thinking":
                    summary = block.get("thinking")
                    self.events.append(
                        Reasoning(
                            message=message.key, summary=summary if isinstance(summary, str) else None, provenance=where
                        )
                    )
                case "tool_use" if (
                    isinstance(call_id := block.get("id"), str)
                    and isinstance(name := block.get("name"), str)
                    and isinstance(arguments := block.get("input"), dict)
                ):
                    self.events.append(
                        ToolCallStarted(
                            message=message.key, call_id=call_id, tool_name=name, arguments=arguments, provenance=where
                        )
                    )
                case block_type:
                    self._unprojected(f"{frames.ASSISTANT_FRAME_KIND}/{block_type}")

    def _message_for(self, frame: RecordedFrame) -> OpenMessage:
        """The message this frame continues, or a new one.

        The run is defined by `message.id` and closed by a different one — not by the next
        non-`assistant` frame, which would split a real message containing a tool result and
        attribute its second call to a message that does not exist. A frame with no id cannot be
        grouped, so it is its own message; the wire supplies one essentially always, the exceptions
        being the console's own reconstructions.
        """
        agent_message_id = frames.agent_message_id(frame.payload)
        if (
            (open_message := self.open_message) is not None
            and agent_message_id is not None
            and open_message.agent_message_id == agent_message_id
        ):
            continued = replace(open_message, last_frame_seq=frame.frame_seq)
            self.open_message = continued
            return continued
        self.close_message()
        started = OpenMessage(
            key=MessageKey(opened_at_frame_seq=frame.frame_seq),
            agent_message_id=agent_message_id,
            last_frame_seq=frame.frame_seq,
            texts=(),
        )
        self.open_message = started
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
                    self.events.append(
                        ToolCallCompleted(
                            call_id=call_id,
                            content=_result_content(block.get("content")),
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
                usage=_usage(frame.payload),
                provenance=FrameRange(frame.frame_seq, frame.frame_seq),
            )
        )

    def _system(self, frame: RecordedFrame) -> None:
        subtype = frame.payload.get("subtype")
        if subtype in _IGNORED_SYSTEM_SUBTYPES:
            return
        where = FrameRange(frame.frame_seq, frame.frame_seq)
        match subtype:
            # `tool_use_id` is the call that opened the task, and only this frame carries it: the
            # terminal report is paired to here by `task_id`, so a link dropped here is gone.
            case "task_started" if (
                isinstance(task_id := frame.payload.get("task_id"), str)
                and isinstance(call_id := frame.payload.get("tool_use_id"), str)
                and isinstance(description := frame.payload.get("description"), str)
            ):
                self.events.append(
                    ActivityStarted(activity_id=task_id, call_id=call_id, description=description, provenance=where)
                )
            case "task_notification" if isinstance(task_id := frame.payload.get("task_id"), str):
                summary = frame.payload.get("summary")
                self.events.append(
                    ActivityCompleted(
                        activity_id=task_id,
                        summary=summary if isinstance(summary, str) else None,
                        # The one status field in the protocol that discriminates: `completed`
                        # ×24 and `failed` ×1 across the corpus.
                        outcome=_activity_outcome(frame.payload.get("status")),
                        provenance=where,
                    )
                )
            case _:
                self._unprojected(f"system/{subtype}")

    def _unprojected(self, key: str) -> None:
        self.unprojected[key] = self.unprojected.get(key, 0) + 1


def _result_content(content: Any) -> ToolResultContent:
    """The renderable half of a tool result.

    Usually a bare string; the rest is a list, and every list in the corpus is `tool_reference`
    blocks naming a tool and carrying nothing else — hence a renderer reading `content` alone shows
    them as empty.
    """
    if isinstance(content, str):
        return TextContent(text=content)
    if isinstance(content, list) and content:
        blocks = [block for block in content if isinstance(block, dict)]
        if len(blocks) == len(content):
            if all(block.get("type") == "tool_reference" for block in blocks):
                return ToolReferences(tool_names=tuple(str(block.get("tool_name")) for block in blocks))
            if all(block.get("type") == "text" and isinstance(block.get("text"), str) for block in blocks):
                return TextContent(text="".join(str(block["text"]) for block in blocks))
    return OpaqueContent(payload=content)


def _result_outcome(is_error: Any) -> Outcome:
    match is_error:
        case True:
            return Outcome.FAILED
        case False:
            return Outcome.SUCCEEDED
        case _:
            # Routinely absent rather than false, so `"is_error" in block` tests nothing.
            return Outcome.UNKNOWN


def _activity_outcome(status: Any) -> Outcome:
    match status:
        case "completed":
            return Outcome.SUCCEEDED
        case "failed":
            return Outcome.FAILED
        case _:
            return Outcome.UNKNOWN


def _usage(payload: Mapping[str, Any]) -> Usage | None:
    """What the turn cost, or None where the frame accounted for nothing.

    None means *no accounting at all*, which is why the three sources are tested together: cost
    and duration are top-level fields of the result and do not live inside `usage`, so keying the
    whole shape on that object's presence would let one field's absence delete another's value.
    A counter it did not carry is 0, as the neutral shape defines an unreported counter.
    """
    reported = payload.get("usage")
    usage: Mapping[str, Any] = reported if isinstance(reported, dict) else {}
    cost = payload.get("total_cost_usd")
    duration = payload.get("duration_ms")
    if not usage and not isinstance(cost, int | float) and not isinstance(duration, int):
        return None
    return Usage(
        input_tokens=_counter(usage.get("input_tokens")),
        output_tokens=_counter(usage.get("output_tokens")),
        cached_input_tokens=_counter(usage.get("cache_read_input_tokens")),
        cost_usd=float(cost) if isinstance(cost, int | float) else None,
        duration_ms=duration if isinstance(duration, int) else None,
    )


def _counter(value: Any) -> int:
    return value if isinstance(value, int) else 0
