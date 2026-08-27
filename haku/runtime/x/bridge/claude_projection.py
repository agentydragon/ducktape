"""Claude Code's native frames into neutral conversation operations, runner-side.

The Claude half of the #4667 boundary: the runner interprets the CLI's own stream and emits the
materialized operations of <neutral_operations.py>, so the Console stops parsing native payloads.
Semantics are ported from the Console projector this replaces at the generation cut
(`haku/console/x/claude_code/projection.py`, deletion-scheduled with its package) — written, as
that fold was, against what the wire does rather than what it documents:

| What the wire does                                             | What this does with it |
| -------------------------------------------------------------- | ---------------------- |
| Text deltas carry no `message.id` to key by                    | A delta joins the open message; one arriving with nothing open opens a message at its own frame |
| Tool arguments stream as partial JSON fragments                | The call is composed privately and opened only by the frame that completes the object — "a call is being composed" is not expressible |
| A tool result can land between two frames of one message       | A `user` frame never closes a message; the item's `FrameRange` spans the interruption |
| A result can precede the completed `assistant` block (2.1.220+)| The composed stream call is finished and answered first; the later block copy is deduplicated by `call_id` |
| The CLI folds a queued prompt into the active turn             | `admit` inside an open turn emits only `prompt.admitted` — the fence, not a second bracket |
| `stop_reason` is never set on `assistant`; `is_error` lies     | A message closes on a different id or `result`; outcomes come from `subtype` and the tri-state `tool_result.is_error` |
| Most of the wire is `system`, and frame classes keep appearing | Ignored by name where deliberate; everything else counts into `Projected.unprojected` — neither a crash nor a silent drop |

**Identity is minted here.** Every turn and item travels under a runner-minted `UUID` that every
later operation repeats; Claude's own names (`msg_…`, `toolu_…`) ride along as `backend_item_id`
provenance. A `thinking` block is a whole reasoning item — opened, segmented and completed by the
one frame that carries it — and `redacted_thinking` completes `WITHHELD` with no segment. Thinking
deltas are deliberately unread: the completed block is the one authority for reasoning prose, as
the completed text block is for a message a delta never streamed.

**Turns are brackets the runner draws.** A prompt admitted while idle opens one (`PromptsCause`);
a content frame arriving while idle opens one as a wake, classified as the Console's watcher did
(`assistant`, `stream_event`, or a `user` frame with text — Claude injecting its own command);
`result` closes whichever is open. A tool result arriving with no turn open completes its known
item without opening one: the answer to an old call is not a new exchange.

**`observe` reads the CLI's stdout only.** The runner's own injected input never goes through it —
an admission is reported by `admit`, which carries the injected frame's seq when the runner
numbered one. Fed its own prompt back, the projector would classify it as the harness waking.

The projector is one CLI process's companion and dies with it — a session stays terminal on runner
loss — so it is a plain stateful object, not a resumable reducer: replay after a Console reconnect
is the journal's (<operation_journal.py>), which retains whole batches, and nothing re-folds
frames from a cursor.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any
from uuid import UUID, uuid4

from haku.runtime.x.bridge.neutral_operations import (
    FrameRange,
    ItemCompleted,
    ItemOpened,
    ItemSegment,
    MessageCompletion,
    MessageOpen,
    Operation,
    PromptAdmitted,
    PromptsCause,
    ReasoningCompletion,
    ReasoningDisclosure,
    ReasoningOpen,
    ToolCallCompletion,
    ToolCallOpen,
    ToolOutcome,
    TurnAnswered,
    TurnEnded,
    TurnFailed,
    TurnOpened,
    WakeCause,
)

# Frame classes that say nothing about the conversation, listed rather than discovered so that a
# class the CLI adds lands in the default branch instead of here: `command_lifecycle` is not a
# clean triple and turns end at `result`; the control channel is another protocol entirely;
# `rate_limit_event` is the account's state, not the conversation's.
_IGNORED_KINDS = frozenset({"command_lifecycle", "control_request", "control_response", "rate_limit_event"})

# The bulk of the log by volume, and none of it conversation: `thinking_tokens` is budget
# accounting, `status` a heartbeat, `init` session identity.
_IGNORED_SYSTEM_SUBTYPES = frozenset({"thinking_tokens", "status", "init"})


@dataclass(frozen=True, slots=True)
class Projected:
    """One observation's neutral yield: operations in order, and what could not be said.

    `unprojected` counts by frame class, in this projector's own vocabulary; deliberately ignored
    classes are not in it. The journal accumulates both into batches.
    """

    operations: tuple[Operation, ...]
    unprojected: Mapping[str, int]


@dataclass(frozen=True, slots=True)
class _OpenMessage:
    """The message being written, including the prose already delivered as segments.

    `last_prose_frame_seq` moves only on frames that contributed words: the completion's range
    reports where the prose came from, and a tool result inside the message does not widen it.
    """

    item_id: UUID
    opened_at_frame_seq: int
    last_prose_frame_seq: int
    backend_item_id: str | None
    delivered: str


@dataclass(frozen=True, slots=True)
class _OpenToolComposition:
    """A tool call whose streamed JSON arguments are not a complete object yet."""

    call_id: str
    tool_name: str
    block_index: int
    opened_at_frame_seq: int
    last_frame_seq: int
    initial_arguments: dict[str, Any]
    partial_json: str


@dataclass(slots=True)
class _Yield:
    """One `observe`/`admit` call's accumulating output."""

    operations: list[Operation] = field(default_factory=list)
    unprojected: dict[str, int] = field(default_factory=dict)

    def miss(self, key: str) -> None:
        self.unprojected[key] = self.unprojected.get(key, 0) + 1

    def projected(self) -> Projected:
        return Projected(operations=tuple(self.operations), unprojected=self.unprojected)


def _at(frame_seq: int) -> FrameRange:
    return FrameRange(first_frame_seq=frame_seq, last_frame_seq=frame_seq)


def _frame_kind(payload: Mapping[str, Any]) -> str:
    kind = payload.get("type")
    if isinstance(kind, str):
        return kind
    method = payload.get("method")
    return method if isinstance(method, str) else "<undiscriminated>"


def _user_text(payload: Mapping[str, Any]) -> str | None:
    """The text of a harness-injected user command, or None for a tool-result user frame."""
    message = payload.get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if isinstance(content, str):
        return content.strip() or None
    if not isinstance(content, list):
        return None
    texts = [str(block.get("text", "")) for block in content if isinstance(block, dict) and block.get("type") == "text"]
    return "\n".join(text for text in texts if text).strip() or None


def _begins_exchange(kind: str, payload: Mapping[str, Any]) -> bool:
    """Whether a frame arriving while no turn is open begins one, as the Console's wake watcher
    classified idle frames: content frames do, a `user` frame only when it carries text — a
    tool-result `user` frame is an old call's answer, not a new exchange."""
    if kind in ("assistant", "stream_event"):
        return True
    return kind == "user" and _user_text(payload) is not None


def _text_delta(event: Mapping[str, Any]) -> str:
    delta = event.get("delta")
    if not isinstance(delta, dict) or delta.get("type") != "text_delta":
        return ""
    text = delta.get("text")
    return text if isinstance(text, str) else ""


def undelivered(text: str, delivered: str) -> str:
    """The part of a completed block nobody has been shown yet.

    A block's deltas deliver its prose as it is written and the completed block repeats all of it,
    so emitting the block whole would say the answer twice. The overlap is subtracted rather than
    a prefix length assumed: the full watermark mid-stream, nothing where a block streamed no
    deltas, and no double print where the two texts disagree in a way a prefix test would miss.
    """
    overlap = min(len(text), len(delivered))
    while overlap and not delivered.endswith(text[:overlap]):
        overlap -= 1
    return text[overlap:]


def _result_content(content: Any) -> str:
    """The renderable half of a tool result, as the text a transcript prints.

    Prose in the two shapes that carry prose — a bare string, and a list of `text` blocks, which
    is what an MCP tool's result arrives as. Everything else is rendered as its JSON: the block
    set is the provider's to extend, and what the call actually produced is in `structured`
    either way.
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
            # Routinely absent rather than false, so presence tests nothing.
            return ToolOutcome.UNKNOWN


class ClaudeProjector:
    """The stateful fold from one CLI's stdout stream to neutral operations.

    Feed every stdout frame to `observe` in the runner's numbering order; report every injected
    prompt to `admit` at the write that injected it. Both return the operations to journal, in
    order, and never raise on wire content: an unreadable frame is counted, not fatal.
    """

    def __init__(self, mint_id: Callable[[], UUID] = uuid4) -> None:
        self._mint_id = mint_id
        self._open_turn: UUID | None = None
        self._open_message: _OpenMessage | None = None
        self._composition: _OpenToolComposition | None = None
        # Runner item ids for calls declared but unanswered, keyed by Claude's `call_id`; settled
        # ids stay known so the completed block's compatibility copy and a repeated result are
        # recognized rather than re-opened.
        self._open_calls: dict[str, UUID] = {}
        self._settled_call_ids: set[str] = set()

    def admit(self, prompt_id: UUID, *, after_batch_seq: int | None, frame_seq: int | None = None) -> Projected:
        """The runner injected Console prompt *prompt_id* into the CLI at this fence.

        *after_batch_seq* is the journal's `admission_frontier` at the moment of injection — the
        last already-numbered batch, which pins the fence against the journal's numbering however
        the operations around it are packaged. *frame_seq* is the injected input frame's own
        number where the runner records one.

        Idle, the admission opens a turn it causes; inside an open turn it is only the fence —
        the CLI folds a queued prompt into the active exchange at a tool boundary, and a second
        bracket would claim an exchange that is not happening.
        """
        provenance = _at(frame_seq) if frame_seq is not None else None
        result = _Yield()
        result.operations.append(
            PromptAdmitted(prompt_id=prompt_id, after_batch_seq=after_batch_seq, provenance=provenance)
        )
        if self._open_turn is None:
            turn_id = self._mint_id()
            self._open_turn = turn_id
            result.operations.append(
                TurnOpened(turn_id=turn_id, cause=PromptsCause(prompt_ids=(prompt_id,)), provenance=provenance)
            )
        return result.projected()

    def observe(self, frame_seq: int, payload: Mapping[str, Any]) -> Projected:
        """Fold one CLI stdout frame, numbered *frame_seq* by the runner."""
        result = _Yield()
        kind = _frame_kind(payload)
        if self._open_turn is None and _begins_exchange(kind, payload):
            turn_id = self._mint_id()
            self._open_turn = turn_id
            result.operations.append(TurnOpened(turn_id=turn_id, cause=WakeCause(), provenance=_at(frame_seq)))
        if kind == "stream_event":
            self._stream_event(result, frame_seq, payload)
        elif kind in _IGNORED_KINDS:
            pass
        elif kind == "assistant":
            self._assistant(result, frame_seq, payload)
        elif kind == "user":
            self._user(result, frame_seq, payload)
        elif kind == "result":
            self._result(result, frame_seq, payload)
        elif kind == "system":
            subtype = payload.get("subtype")
            if subtype not in _IGNORED_SYSTEM_SUBTYPES:
                result.miss(f"system/{subtype}")
        else:
            result.miss(kind)
        return result.projected()

    def _stream_event(self, result: _Yield, frame_seq: int, payload: Mapping[str, Any]) -> None:
        """One increment of prose or tool arguments still being written.

        Claude Code 2.1.220 can execute a tool and return its result before emitting the completed
        `assistant` block that used to declare the call, so the stream's start, JSON fragments and
        stop are the first complete account of the call — not a lower-granularity copy. Thinking
        and signature deltas are deliberately unread; the completed `thinking` block carries the
        one authoritative copy of that prose.
        """
        event = payload.get("event")
        if not isinstance(event, dict):
            return
        match event.get("type"):
            case "content_block_start":
                block = event.get("content_block")
                index = event.get("index")
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    return
                call_id, name, arguments = block.get("id"), block.get("name"), block.get("input")
                if not (
                    isinstance(index, int)
                    and isinstance(call_id, str)
                    and isinstance(name, str)
                    and isinstance(arguments, dict)
                ):
                    result.miss("stream_event/tool_use_start")
                    return
                if self._composition is not None:
                    result.miss("stream_event/overlapping_tool_use")
                self._composition = _OpenToolComposition(
                    call_id=call_id,
                    tool_name=name,
                    block_index=index,
                    opened_at_frame_seq=frame_seq,
                    last_frame_seq=frame_seq,
                    initial_arguments=dict(arguments),
                    partial_json="",
                )
            case "content_block_delta":
                delta = event.get("delta")
                if (
                    isinstance(delta, dict)
                    and delta.get("type") == "input_json_delta"
                    and (composing := self._composition) is not None
                    and event.get("index") == composing.block_index
                ):
                    partial = delta.get("partial_json")
                    if isinstance(partial, str):
                        self._composition = replace(
                            composing, last_frame_seq=frame_seq, partial_json=composing.partial_json + partial
                        )
                    else:
                        result.miss("stream_event/input_json_delta")
                    return
                self._stream_text(result, frame_seq, event)
            case "content_block_stop":
                if (composing := self._composition) is not None and event.get("index") == composing.block_index:
                    self._finish_composition(result, last_frame_seq=frame_seq)

    def _stream_text(self, result: _Yield, frame_seq: int, event: Mapping[str, Any]) -> None:
        """One increment of an answer still being written.

        **It attaches to the open message rather than keying itself.** A delta carries no
        `message.id` to group by, and prose belongs to an item — so a delta arriving with nothing
        open opens a message at its own frame, and every delta after it joins that one. The CLI
        writes one answer at a time, which is what makes "the open one" unambiguous.
        """
        if not (text := _text_delta(event)):
            return
        message = self._open_message
        if message is None:
            message = _OpenMessage(
                item_id=self._mint_id(),
                opened_at_frame_seq=frame_seq,
                last_prose_frame_seq=frame_seq,
                backend_item_id=None,
                delivered="",
            )
            result.operations.append(
                ItemOpened(
                    item_id=message.item_id, turn_id=self._open_turn, item=MessageOpen(), provenance=_at(frame_seq)
                )
            )
        self._open_message = replace(message, last_prose_frame_seq=frame_seq, delivered=message.delivered + text)
        result.operations.append(ItemSegment(item_id=message.item_id, text=text, provenance=_at(frame_seq)))

    def _finish_composition(self, result: _Yield, *, last_frame_seq: int) -> None:
        """Open the complete call being composed, or count a malformed composition."""
        composing = self._composition
        if composing is None:
            return
        self._composition = None
        arguments: Any = composing.initial_arguments
        if composing.partial_json:
            try:
                arguments = json.loads(composing.partial_json)
            except json.JSONDecodeError:
                result.miss("stream_event/tool_use_arguments")
                return
        if not isinstance(arguments, dict):
            result.miss("stream_event/tool_use_arguments")
            return
        self._declare_call(
            result,
            call_id=composing.call_id,
            tool_name=composing.tool_name,
            arguments=arguments,
            provenance=FrameRange(first_frame_seq=composing.opened_at_frame_seq, last_frame_seq=last_frame_seq),
        )

    def _declare_call(
        self, result: _Yield, *, call_id: str, tool_name: str, arguments: dict[str, Any], provenance: FrameRange
    ) -> None:
        """Open one call once, whichever of the stream or the completed block declared it first."""
        if call_id in self._open_calls or call_id in self._settled_call_ids:
            return
        item_id = self._mint_id()
        self._open_calls[call_id] = item_id
        result.operations.append(
            ItemOpened(
                item_id=item_id,
                turn_id=self._open_turn,
                item=ToolCallOpen(tool_name=tool_name, arguments=arguments),
                backend_item_id=call_id,
                provenance=provenance,
            )
        )

    def _assistant(self, result: _Yield, frame_seq: int, payload: Mapping[str, Any]) -> None:
        where = _at(frame_seq)
        message = payload.get("message")
        backend_item_id = str(agent_id) if isinstance(message, dict) and (agent_id := message.get("id")) else None
        # **A different `message.id` ends the message before it, whatever this frame carries.** The
        # run is defined by the id, not by which block types happen to be in the frame that breaks
        # it — a frame of pure thinking closes the previous answer here rather than leaving it open
        # until the next frame with prose in it.
        if (
            (open_message := self._open_message) is not None
            and backend_item_id is not None
            and open_message.backend_item_id not in (None, backend_item_id)
        ):
            self._close_message(result)
        content = message.get("content") if isinstance(message, dict) else None
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            match block.get("type"):
                case "text" if isinstance(text := block.get("text"), str):
                    continued = self._message_for(result, frame_seq, backend_item_id)
                    if remainder := undelivered(text, continued.delivered):
                        result.operations.append(
                            ItemSegment(item_id=continued.item_id, text=remainder, provenance=where)
                        )
                    # The block is finished, so the next one starts undelivered.
                    self._open_message = replace(continued, delivered="")
                case "thinking":
                    # Opened, spoken and closed by the one frame that carries it: Claude nests
                    # reasoning inside an assistant message, and unnesting it here keeps that one
                    # backend's shape out of the record.
                    summary = block.get("thinking")
                    item_id = self._mint_id()
                    result.operations.append(
                        ItemOpened(item_id=item_id, turn_id=self._open_turn, item=ReasoningOpen(), provenance=where)
                    )
                    if isinstance(summary, str) and summary:
                        result.operations.append(ItemSegment(item_id=item_id, text=summary, provenance=where))
                    result.operations.append(
                        ItemCompleted(
                            item_id=item_id,
                            completion=ReasoningCompletion(disclosure=ReasoningDisclosure.SUMMARY),
                            provenance=where,
                        )
                    )
                case "redacted_thinking":
                    # The model thought and none of it is available: an item with no segments,
                    # which is the one thing an empty string could not say.
                    item_id = self._mint_id()
                    result.operations.append(
                        ItemOpened(item_id=item_id, turn_id=self._open_turn, item=ReasoningOpen(), provenance=where)
                    )
                    result.operations.append(
                        ItemCompleted(
                            item_id=item_id,
                            completion=ReasoningCompletion(disclosure=ReasoningDisclosure.WITHHELD),
                            provenance=where,
                        )
                    )
                case "tool_use" if (
                    isinstance(call_id := block.get("id"), str)
                    and isinstance(name := block.get("name"), str)
                    and isinstance(arguments := block.get("input"), dict)
                ):
                    self._declare_call(result, call_id=call_id, tool_name=name, arguments=arguments, provenance=where)
                    if self._composition is not None and self._composition.call_id == call_id:
                        # The completed block already said everything the composition was building.
                        self._composition = None
                case block_type:
                    result.miss(f"assistant/{block_type}")

    def _message_for(self, result: _Yield, frame_seq: int, backend_item_id: str | None) -> _OpenMessage:
        """The message this frame continues, or a new one.

        The run is defined by `message.id` and closed by a different one — not by the next
        non-`assistant` frame, which would split a real message containing a tool result. A frame
        with no id cannot be grouped, so it is its own message — except that **a message being
        streamed into absorbs the frame that completes it, id or no id**: deltas carry no id, so
        the message they opened has none until a completed block gives it one, and treating that
        as a different id would close the item the operator watched arrive and open an empty
        second one for the same prose.
        """
        open_message = self._open_message
        if open_message is not None and (
            open_message.backend_item_id == backend_item_id
            if open_message.backend_item_id is not None
            else bool(open_message.delivered)
        ):
            continued = replace(open_message, last_prose_frame_seq=frame_seq, backend_item_id=backend_item_id)
            self._open_message = continued
            return continued
        self._close_message(result)
        item_id = self._mint_id()
        started = _OpenMessage(
            item_id=item_id,
            opened_at_frame_seq=frame_seq,
            last_prose_frame_seq=frame_seq,
            backend_item_id=backend_item_id,
            delivered="",
        )
        self._open_message = started
        result.operations.append(
            ItemOpened(
                item_id=item_id,
                turn_id=self._open_turn,
                item=MessageOpen(),
                backend_item_id=backend_item_id,
                provenance=_at(frame_seq),
            )
        )
        return started

    def _close_message(self, result: _Yield) -> None:
        """End the open message, if there is one. Called where wire semantics say a message ends —
        a different `message.id`, or a `result` — and nowhere else: running out of frames is not
        one of those, and nothing may declare the stream over."""
        open_message = self._open_message
        if open_message is None:
            return
        self._open_message = None
        result.operations.append(
            ItemCompleted(
                item_id=open_message.item_id,
                completion=MessageCompletion(),
                backend_item_id=open_message.backend_item_id,
                provenance=FrameRange(
                    first_frame_seq=open_message.opened_at_frame_seq, last_frame_seq=open_message.last_prose_frame_seq
                ),
            )
        )

    def _user(self, result: _Yield, frame_seq: int, payload: Mapping[str, Any]) -> None:
        """A tool result coming back, or an injected command the CLI chose to echo.

        The content type gives the direction: text content is the harness speaking (already
        classified as a wake where it began an exchange) and projects to nothing; a list carries
        `tool_result` blocks, answered here by the runner id the call was opened under.
        """
        message = payload.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, str):
            return
        if not isinstance(content, list):
            result.miss("user")
            return
        # Top-level and undocumented, and the channel the tool's real output arrives on.
        structured = payload.get("tool_use_result")
        where = _at(frame_seq)
        for block in content:
            match block.get("type") if isinstance(block, dict) else None:
                case "tool_result" if isinstance(call_id := block.get("tool_use_id"), str):
                    if self._composition is not None and self._composition.call_id == call_id:
                        # The result outran the stop of the block composing the call: the fragments
                        # already seen are the whole account there will be.
                        self._finish_composition(result, last_frame_seq=self._composition.last_frame_seq)
                    if (item_id := self._open_calls.pop(call_id, None)) is None:
                        result.miss(
                            "user/repeated_tool_result"
                            if call_id in self._settled_call_ids
                            else "user/tool_result_without_call"
                        )
                        continue
                    self._settled_call_ids.add(call_id)
                    if rendered := _result_content(block.get("content")):
                        result.operations.append(ItemSegment(item_id=item_id, text=rendered, provenance=where))
                    result.operations.append(
                        ItemCompleted(
                            item_id=item_id,
                            completion=ToolCallCompletion(
                                outcome=_result_outcome(block.get("is_error")), structured=structured
                            ),
                            provenance=where,
                        )
                    )
                case block_type:
                    result.miss(f"user/{block_type}")

    def _result(self, result: _Yield, frame_seq: int, payload: Mapping[str, Any]) -> None:
        """The frame that ends a turn: close the open message, then the bracket.

        The outcome comes from `subtype` alone — `is_error` is false on every production result,
        including sessions recorded as failed. Claude states no abort here: an operator's stop
        arrives as a `result` like any other, and the side that asked records the abort.
        """
        self._close_message(result)
        if self._composition is not None:
            # A composition the wire never finished; there is no complete object to open a call
            # with, and the next turn must not inherit half of this one's arguments.
            self._composition = None
            result.miss("stream_event/unfinished_tool_use")
        if (turn_id := self._open_turn) is None:
            result.miss("result/no_open_turn")
            return
        self._open_turn = None
        subtype = payload.get("subtype")
        stop_reason = payload.get("stop_reason")
        stated = stop_reason if isinstance(stop_reason, str) and stop_reason else "unknown error"
        end = TurnAnswered() if subtype == "success" else TurnFailed(failure=f"{subtype}: {stated}")
        result.operations.append(TurnEnded(turn_id=turn_id, end=end, provenance=_at(frame_seq)))
