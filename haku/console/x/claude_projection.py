"""Claude CLI frames into neutral conversation events.

One pure function, `project`. Determinism is the property the whole design rests on: the same
frames always project to the same events, so re-projecting a stored session reproduces its stored
rows exactly, drift is detectable by comparing them, and a projection bug is repairable by fixing
the fold rather than being baked into a row forever
(<../../plans/chat_runtime_projection.md> § stage 4).

`claude_chat.py`'s turn loop is the one caller: it projects each frame as it lands and acts on the
events. Nothing stores them yet — the durable cursor beside them is the other half of stage 4.

**Written against what the wire does, not what it documents.** Every rule below that looks
defensive is a finding from <../debug/frame_shape_census.md>, which is where the measurements
live — a share of production frames is a dated observation and belongs in a dated document, not
in a docstring that will still claim it a year from now.

| What the wire does                                              | What this does with it                                                                                                                    |
| ---------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------ |
| One block per `assistant` frame, so a message spans several      | A message is the run of frames sharing `message.id`, closed by a *different* id, a `result`, or the end of the sequence — never by a clock |
| `stop_reason` is never set                                       | Nothing reads it; there is no "the provider said it was done" branch to be wrong                                                           |
| A `user` frame can land between two frames of one message        | A `user` frame never closes a message. Its `FrameRange` spans the interruption, which is what a range means                                |
| `content` is usually prose, sometimes tool names and no payload  | `content` is a variant, and the real output rides beside it as `structured` (`tool_use_result`, many per-tool shapes)                      |
| `is_error` is often absent, and `result.is_error` is never true  | `Outcome.UNKNOWN` where it is absent, and a turn's outcome comes from `subtype` — `is_error` is read nowhere                               |
| Most of the wire is `system`, and most of that is one constant   | `_IGNORED_KINDS` / `_IGNORED_SYSTEM_SUBTYPES` are frozenset lookups that return before anything is allocated                               |
| Frame classes and `system` subtypes exist that `protocol.md` omits | The default branch counts into `Projection.unprojected` — neither a crash nor a silent drop                                                |
| `command_lifecycle` is not a clean triple                        | It is not read at all: turn boundaries come from `result`, so no sequence assumption exists to be violated                                 |

Two decisions worth knowing before reading the code.

**Where a `TextDelta` is cut is the caller's to choose, and it is the only thing the wire and the
log disagree about** — see `DeltaSource`. Reading a stored log, deltas cannot be the source: most
sessions emit none at all, so a consumer built on them renders nothing for those; where they do
occur they multiply the row count and are mostly `input_json_delta` — tool arguments rather than
prose — and they carry no identity, so `frame_identity.py` deliberately refuses to dedupe them.
Decisively, a log truncated mid-block would re-project to different text than the completed block
it precedes. A live consumer has the increments in hand and wants them as they arrive, which is
what streaming an answer *is*. The prose is the same either way; only its granularity differs, and
a backend that streams meaningfully can cut it finer without any consumer changing.

**`result.result` is not projected as prose.** It repeats the final message, so minting one from it
would double every answer. A turn that produced no `MessageCompleted` said nothing, which is a fact
worth being able to see.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from haku.console.chat_models import TurnOutcome
from haku.console.x import session_frames
from haku.console.x.conversation_events import (
    ActivityCompleted,
    ActivityStarted,
    ConversationEvent,
    FrameRange,
    Json,
    MessageCompleted,
    MessageKey,
    OpaqueContent,
    Outcome,
    Projection,
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

    `COMPLETED_BLOCKS` is the only honest reading of a *stored* log — `stream_event` frames carry
    no identity, are not deduplicated, and a log truncated mid-block would re-project to different
    text than the completed block that follows it, which is the determinism the whole design rests
    on. `STREAM_EVENTS` is what a consumer holding the live wire drives: it takes the increments as
    they arrive and skips the completed block's own text, which those increments already delivered.
    """

    COMPLETED_BLOCKS = "completed_blocks"
    STREAM_EVENTS = "stream_events"


@dataclass(frozen=True, slots=True)
class RecordedFrame:
    """One row of the frame log: a CLI protocol frame and where it sits in the session.

    `payload` is the wire verbatim, so it is read with `.get` and type guards throughout — a
    frame this release has never seen is a new CLI feature, not a bug in us. A row the CLI did
    not author (the bridge's `setup_output`, the console's own `partial`) does not belong here
    and `payload["type"]` is what says so.
    """

    frame_seq: int
    payload: dict[str, Any]


def project(frames: Iterable[RecordedFrame], *, delta_source: DeltaSource = DeltaSource.COMPLETED_BLOCKS) -> Projection:
    """Fold a session's frames — all of them, a suffix from a cursor, or the one just received —
    into neutral events.

    Pure and order-dependent: the events are a function of the sequence, not of any one frame.
    Raises `ValueError` on a payload with no `type`, which is a caller handing it a row the CLI
    never sent rather than anything the wire can do.
    """
    projector = _Projector(delta_source=delta_source)
    for frame in frames:
        projector.fold(frame)
    # An open message at the end of the input is one whose turn died mid-answer — three real
    # commands ended that way. It completes with what it had; the alternative is losing it.
    projector.close_message()
    return Projection(events=tuple(projector.events), unprojected=MappingProxyType(dict(projector.unprojected)))


@dataclass(slots=True)
class _OpenMessage:
    key: MessageKey
    agent_message_id: str | None
    last_frame_seq: int
    texts: list[str]


@dataclass(slots=True)
class _Projector:
    delta_source: DeltaSource
    events: list[ConversationEvent] = field(default_factory=list)
    unprojected: dict[str, int] = field(default_factory=dict)
    open_message: _OpenMessage | None = None

    def fold(self, frame: RecordedFrame) -> None:
        kind = session_frames.frame_kind(frame.payload)
        if kind == session_frames.DELTA_FRAME_KIND:
            if self.delta_source is DeltaSource.STREAM_EVENTS:
                self._stream_delta(frame)
            return
        if kind in _IGNORED_KINDS:
            return
        match kind:
            case session_frames.ASSISTANT_FRAME_KIND:
                self._assistant(frame)
            case session_frames.PROMPT_FRAME_KIND:
                self._user(frame)
            case session_frames.RESULT_FRAME_KIND:
                self._result(frame)
            case "system":
                self._system(frame)
            case _:
                self._unprojected(kind)

    def close_message(self) -> None:
        """End the open message, if there is one. Called where the census says a message ends —
        a different `message.id`, a `result`, the end of the input — and nowhere else."""
        if (open_message := self.open_message) is None:
            return
        self.open_message = None
        self.events.append(
            MessageCompleted(
                message=open_message.key,
                # Joined bare, because the deltas above are increments of one answer rather than
                # paragraphs of it.
                text="".join(open_message.texts) or None,
                agent_message_id=open_message.agent_message_id,
                provenance=FrameRange(open_message.key.opened_at_frame_seq, open_message.last_frame_seq),
            )
        )

    def _stream_delta(self, frame: RecordedFrame) -> None:
        """One increment of an answer still being written, for a consumer holding the live wire.

        **It opens no message.** A delta carries no `message.id`, so there is nothing to group it
        by; the completed block that follows is what says which message the prose belonged to. Its
        `MessageKey` is therefore the delta's own frame, which is enough for a consumer tracking
        one open message at a time — and every live one does, since a CLI writes one answer at a
        time.
        """
        event = frame.payload.get("event")
        if not isinstance(event, dict) or not (text := session_frames.text_delta(event)):
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
        for block in session_frames.content_blocks(frame.payload):
            match block.get("type"):
                case "text" if isinstance(text := block.get("text"), str):
                    message.texts.append(text)
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
                    self._unprojected(f"{session_frames.ASSISTANT_FRAME_KIND}/{block_type}")

    def _message_for(self, frame: RecordedFrame) -> _OpenMessage:
        """The message this frame continues, or a new one.

        The run is defined by `message.id` and closed by a different one — not by the next
        non-`assistant` frame, which would split a real message that has a tool result inside it
        and attribute its second call to a message that does not exist. A frame with no id cannot
        be grouped, so it is its own message; the wire supplies one essentially always, and the
        exceptions are the console's own reconstructions.
        """
        agent_message_id = session_frames.agent_message_id(frame.payload)
        if (
            (open_message := self.open_message) is not None
            and agent_message_id is not None
            and open_message.agent_message_id == agent_message_id
        ):
            open_message.last_frame_seq = frame.frame_seq
            return open_message
        self.close_message()
        self.open_message = _OpenMessage(
            key=MessageKey(opened_at_frame_seq=frame.frame_seq),
            agent_message_id=agent_message_id,
            last_frame_seq=frame.frame_seq,
            texts=[],
        )
        return self.open_message

    def _user(self, frame: RecordedFrame) -> None:
        """A tool result coming back, or the console's own prompt going out.

        Direction is what the content type says, without exception in the corpus: an outbound
        prompt carries a string, an inbound frame carries a list. An outbound prompt projects to
        nothing here — it is the console's own text, which the console already holds.
        """
        message = frame.payload.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, str):
            return
        if not isinstance(content, list):
            self._unprojected(session_frames.PROMPT_FRAME_KIND)
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
                    self._unprojected(f"{session_frames.PROMPT_FRAME_KIND}/{block_type}")

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
            case "task_started" if isinstance(task_id := frame.payload.get("task_id"), str) and isinstance(
                description := frame.payload.get("description"), str
            ):
                self.events.append(ActivityStarted(activity_id=task_id, description=description, provenance=where))
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
    blocks that name a tool and carry nothing else — which is why a renderer reading `content`
    alone shows them as empty.
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
    if not isinstance(usage := payload.get("usage"), dict):
        return None
    cost = payload.get("total_cost_usd")
    duration = payload.get("duration_ms")
    return Usage(
        input_tokens=_counter(usage.get("input_tokens")),
        output_tokens=_counter(usage.get("output_tokens")),
        cached_input_tokens=_counter(usage.get("cache_read_input_tokens")),
        cost_usd=float(cost) if isinstance(cost, int | float) else None,
        duration_ms=duration if isinstance(duration, int) else None,
    )


def _counter(value: Any) -> int:
    return value if isinstance(value, int) else 0
