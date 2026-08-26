"""Codex app-server notifications into Haku's backend-neutral conversation events.

Pinned protocol evidence: ``@openai/codex@0.144.1`` / upstream tag ``rust-v0.144.1``.  The exact
schema and source references are recorded in ``docs/protocol_evidence.md``.

The provider runtime wraps this reducer for live frames and complete-log reprojection. Native
methods and item shapes remain private to this package; only neutral conversation events leave it.

Only facts represented by the existing conversation vocabulary are projected:

* ``agentMessage`` -> message lifecycle and text segments;
* ``reasoning`` summary -> reasoning lifecycle and summary segments;
* ``commandExecution`` and ``mcpToolCall`` -> tool-call lifecycles;
* ``turn/completed`` -> the neutral terminal turn outcome.

Codex-specific item classes (file changes, plans, web search, and newer additions) are counted in
``Projection.unprojected`` instead of crashing or being promoted into a competing vocabulary.
Notifications deliberately rejected by the conversation design (token usage, thread status,
approval progress, and MCP progress narration) are ignored explicitly.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Any

from haku.console.chat_models import ItemType, ReasoningDisclosure, ToolOutcome
from haku.console.x.codex_app_server import frames
from haku.console.x.codex_app_server.protocol import Notification, Request, Response, UnknownMessage, parse_message
from haku.console.x.conversation_events import (
    CallRef,
    ConversationEvent,
    FrameRange,
    ItemSegment,
    Json,
    MessageCompleted,
    MessageStarted,
    OpenRef,
    Projection,
    ReasoningCompleted,
    ReasoningStarted,
    ToolCallCompleted,
    ToolCallStarted,
    TurnAborted,
    TurnAnswered,
    TurnCompleted,
    TurnEnd,
    TurnFailed,
)


@dataclass(frozen=True, slots=True)
class OpenItem:
    """A Codex item seen starting but not ending."""

    opened_at_frame_seq: int
    last_frame_seq: int
    backend_item_id: str | None
    delivered: str = ""


@dataclass(frozen=True, slots=True)
class ProjectionState:
    """Codex-private state carried between app-server message batches."""

    open_message: OpenItem | None = None
    open_reasoning: OpenItem | None = None
    seen_call_ids: frozenset[str] = frozenset()
    completed_call_ids: frozenset[str] = frozenset()


_IGNORED_METHODS = frozenset(
    {
        "thread/started",
        "thread/status/changed",
        "thread/tokenUsage/updated",
        "turn/started",
        "item/reasoning/summaryPartAdded",
        "item/mcpToolCall/progress",
        "serverRequest/resolved",
    }
)

_MESSAGE = OpenRef(item_type=ItemType.MESSAGE)
_REASONING = OpenRef(item_type=ItemType.REASONING)


@dataclass(frozen=True, slots=True)
class RecordedFrame:
    """One app-server message and its durable position in the native frame log."""

    frame_seq: int
    payload: dict[str, Any]


def project(state: ProjectionState, frames: Iterable[RecordedFrame]) -> tuple[ProjectionState, Projection]:
    """Fold server messages into neutral events, preserving state across arbitrary batches."""
    projector = _Projector(
        open_message=state.open_message,
        open_reasoning=state.open_reasoning,
        seen_call_ids=set(state.seen_call_ids),
        completed_call_ids=set(state.completed_call_ids),
    )
    for frame in frames:
        projector.fold(frame)
    return (
        ProjectionState(
            open_message=projector.open_message,
            open_reasoning=projector.open_reasoning,
            seen_call_ids=frozenset(projector.seen_call_ids),
            completed_call_ids=frozenset(projector.completed_call_ids),
        ),
        projector.projected(),
    )


def finish(state: ProjectionState) -> Projection:
    """Close prose items when a caller knows no more native frames can arrive."""
    events: list[ConversationEvent] = []
    if state.open_message is not None:
        events.append(
            MessageCompleted(backend_item_id=state.open_message.backend_item_id, provenance=_span(state.open_message))
        )
    if state.open_reasoning is not None:
        events.append(
            ReasoningCompleted(disclosure=ReasoningDisclosure.SUMMARY, provenance=_span(state.open_reasoning))
        )
    return Projection(events=tuple(events), unprojected=MappingProxyType({}))


def project_log(frames: Iterable[RecordedFrame]) -> Projection:
    """Project a complete native message log; repeated calls on the same log are identical."""
    state, projected = project(ProjectionState(), frames)
    return projected.then(finish(state))


@dataclass(slots=True)
class _Projector:
    open_message: OpenItem | None
    open_reasoning: OpenItem | None
    seen_call_ids: set[str]
    completed_call_ids: set[str]
    events: list[ConversationEvent] = field(default_factory=list)
    unprojected: dict[str, int] = field(default_factory=dict)

    def projected(self) -> Projection:
        return Projection(events=tuple(self.events), unprojected=MappingProxyType(dict(self.unprojected)))

    def fold(self, frame: RecordedFrame) -> None:
        message = parse_message(frame.payload)
        if isinstance(message, (Request, Response)):
            return
        if isinstance(message, UnknownMessage):
            self._unprojected(message.reason)
            return
        assert isinstance(message, Notification)
        if message.method in _IGNORED_METHODS:
            return
        params = message.params
        if params is None:
            self._unprojected(f"{message.method}/params")
            return
        match message.method:
            case "item/started":
                self._item_started(frame.frame_seq, params)
            case "item/agentMessage/delta":
                self._message_delta(frame.frame_seq, params)
            case "item/reasoning/summaryTextDelta":
                self._reasoning_delta(frame.frame_seq, params)
            case "item/commandExecution/outputDelta":
                self._command_delta(frame.frame_seq, params)
            case "item/completed":
                self._item_completed(frame.frame_seq, params)
            case frames.TURN_COMPLETED:
                self._turn_completed(frame.frame_seq, params)
            case _:
                self._unprojected(message.method)

    def _item_started(self, frame_seq: int, params: Mapping[str, Any]) -> None:
        item = _item(params)
        if item is None:
            self._unprojected("item/started/item")
            return
        item_type, item_id = item.get("type"), item.get("id")
        if not isinstance(item_type, str) or not isinstance(item_id, str):
            self._unprojected("item/started/identity")
            return
        if item_type == "agentMessage":
            if self.open_message is not None and self.open_message.backend_item_id not in (None, item_id):
                self._close_message()
            if self.open_message is None:
                self.open_message = OpenItem(frame_seq, frame_seq, item_id)
                self.events.append(MessageStarted(provenance=FrameRange(frame_seq, frame_seq)))
            elif self.open_message.backend_item_id is None:
                self.open_message = replace(self.open_message, last_frame_seq=frame_seq, backend_item_id=item_id)
            return
        if item_type == "reasoning":
            if self.open_reasoning is not None and self.open_reasoning.backend_item_id not in (None, item_id):
                self._close_reasoning()
            if self.open_reasoning is None:
                self.open_reasoning = OpenItem(frame_seq, frame_seq, item_id)
                self.events.append(ReasoningStarted(provenance=FrameRange(frame_seq, frame_seq)))
            elif self.open_reasoning.backend_item_id is None:
                self.open_reasoning = replace(self.open_reasoning, last_frame_seq=frame_seq, backend_item_id=item_id)
            return
        if item_type in {"commandExecution", "mcpToolCall"}:
            self._start_tool(item, frame_seq)
            return
        # userMessage is the request-side prompt, authored before a backend frame claims it.
        if item_type == "userMessage":
            return
        self._unprojected(f"item/started/{item_type}")

    def _message_delta(self, frame_seq: int, params: Mapping[str, Any]) -> None:
        item_id, delta = params.get("itemId"), params.get("delta")
        if not isinstance(item_id, str) or not isinstance(delta, str):
            self._unprojected("item/agentMessage/delta/shape")
            return
        if self.open_message is None:
            self.open_message = OpenItem(frame_seq, frame_seq, item_id)
            self.events.append(MessageStarted(provenance=FrameRange(frame_seq, frame_seq)))
        elif self.open_message.backend_item_id is None:
            self.open_message = replace(self.open_message, backend_item_id=item_id)
        if self.open_message.backend_item_id != item_id:
            self._unprojected("item/agentMessage/delta/itemId")
            return
        self.open_message = replace(
            self.open_message, last_frame_seq=frame_seq, delivered=self.open_message.delivered + delta
        )
        if delta:
            self.events.append(ItemSegment(item=_MESSAGE, text=delta, provenance=FrameRange(frame_seq, frame_seq)))

    def _reasoning_delta(self, frame_seq: int, params: Mapping[str, Any]) -> None:
        item_id, delta = params.get("itemId"), params.get("delta")
        if not isinstance(item_id, str) or not isinstance(delta, str):
            self._unprojected("item/reasoning/summaryTextDelta/shape")
            return
        if self.open_reasoning is None:
            self.open_reasoning = OpenItem(frame_seq, frame_seq, item_id)
            self.events.append(ReasoningStarted(provenance=FrameRange(frame_seq, frame_seq)))
        elif self.open_reasoning.backend_item_id is None:
            self.open_reasoning = replace(self.open_reasoning, backend_item_id=item_id)
        if self.open_reasoning.backend_item_id != item_id:
            self._unprojected("item/reasoning/summaryTextDelta/itemId")
            return
        self.open_reasoning = replace(
            self.open_reasoning, last_frame_seq=frame_seq, delivered=self.open_reasoning.delivered + delta
        )
        if delta:
            self.events.append(ItemSegment(item=_REASONING, text=delta, provenance=FrameRange(frame_seq, frame_seq)))

    def _command_delta(self, frame_seq: int, params: Mapping[str, Any]) -> None:
        item_id, delta = params.get("itemId"), params.get("delta")
        if not isinstance(item_id, str) or not isinstance(delta, str):
            self._unprojected("item/commandExecution/outputDelta/shape")
            return
        if item_id not in self.seen_call_ids:
            self._unprojected("item/commandExecution/outputDelta/itemId")
            return
        if delta:
            self.events.append(
                ItemSegment(item=CallRef(call_id=item_id), text=delta, provenance=FrameRange(frame_seq, frame_seq))
            )

    def _item_completed(self, frame_seq: int, params: Mapping[str, Any]) -> None:
        item = _item(params)
        if item is None:
            self._unprojected("item/completed/item")
            return
        item_type, item_id = item.get("type"), item.get("id")
        if not isinstance(item_type, str) or not isinstance(item_id, str):
            self._unprojected("item/completed/identity")
            return
        if item_type == "agentMessage":
            self._complete_message(item, frame_seq)
            return
        if item_type == "reasoning":
            self._complete_reasoning(item, frame_seq)
            return
        if item_type in {"commandExecution", "mcpToolCall"}:
            if item_id not in self.seen_call_ids:
                self._start_tool(item, frame_seq)
            self._complete_tool(item, frame_seq)
            return
        if item_type == "userMessage":
            return
        self._unprojected(f"item/completed/{item_type}")

    def _complete_message(self, item: Mapping[str, Any], frame_seq: int) -> None:
        item_id, text = item.get("id"), item.get("text")
        assert isinstance(item_id, str)
        if not isinstance(text, str):
            self._unprojected("item/completed/agentMessage/text")
            return
        if self.open_message is None or self.open_message.backend_item_id not in (None, item_id):
            if self.open_message is not None:
                self._close_message()
            self.open_message = OpenItem(frame_seq, frame_seq, item_id)
            self.events.append(MessageStarted(provenance=FrameRange(frame_seq, frame_seq)))
        assert self.open_message is not None
        if self.open_message.backend_item_id is None:
            self.open_message = replace(self.open_message, backend_item_id=item_id)
        suffix = _undelivered(text, self.open_message.delivered)
        if suffix:
            self.events.append(ItemSegment(item=_MESSAGE, text=suffix, provenance=FrameRange(frame_seq, frame_seq)))
        self.open_message = replace(
            self.open_message, last_frame_seq=frame_seq, delivered=self.open_message.delivered + suffix
        )
        self._close_message()

    def _complete_reasoning(self, item: Mapping[str, Any], frame_seq: int) -> None:
        item_id, summary = item.get("id"), item.get("summary")
        assert isinstance(item_id, str)
        if not isinstance(summary, list) or not all(isinstance(part, str) for part in summary):
            self._unprojected("item/completed/reasoning/summary")
            return
        text = "\n\n".join(summary)
        if self.open_reasoning is None or self.open_reasoning.backend_item_id not in (None, item_id):
            if self.open_reasoning is not None:
                self._close_reasoning()
            self.open_reasoning = OpenItem(frame_seq, frame_seq, item_id)
            self.events.append(ReasoningStarted(provenance=FrameRange(frame_seq, frame_seq)))
        assert self.open_reasoning is not None
        if self.open_reasoning.backend_item_id is None:
            self.open_reasoning = replace(self.open_reasoning, backend_item_id=item_id)
        suffix = _undelivered(text, self.open_reasoning.delivered)
        if suffix:
            self.events.append(ItemSegment(item=_REASONING, text=suffix, provenance=FrameRange(frame_seq, frame_seq)))
        self.open_reasoning = replace(
            self.open_reasoning, last_frame_seq=frame_seq, delivered=self.open_reasoning.delivered + suffix
        )
        self._close_reasoning()

    def _start_tool(self, item: Mapping[str, Any], frame_seq: int) -> None:
        item_type, item_id = item.get("type"), item.get("id")
        assert isinstance(item_type, str)
        assert isinstance(item_id, str)
        if item_id in self.seen_call_ids:
            return
        if item_type == "commandExecution":
            command, cwd = item.get("command"), item.get("cwd")
            if not isinstance(command, str) or not isinstance(cwd, str):
                self._unprojected("item/started/commandExecution/shape")
                return
            tool_name = "commandExecution"
            arguments: Mapping[str, Json] = {"command": command, "cwd": cwd}
        else:
            server, tool, arguments_value = item.get("server"), item.get("tool"), item.get("arguments")
            if not isinstance(server, str) or not isinstance(tool, str) or not isinstance(arguments_value, dict):
                self._unprojected("item/started/mcpToolCall/shape")
                return
            tool_name = f"{server}/{tool}"
            arguments = arguments_value
        self.seen_call_ids.add(item_id)
        self.events.append(
            ToolCallStarted(
                call_id=item_id, tool_name=tool_name, arguments=arguments, provenance=FrameRange(frame_seq, frame_seq)
            )
        )

    def _complete_tool(self, item: Mapping[str, Any], frame_seq: int) -> None:
        item_type, item_id = item.get("type"), item.get("id")
        assert isinstance(item_type, str)
        assert isinstance(item_id, str)
        if item_id not in self.seen_call_ids:
            return
        if item_id in self.completed_call_ids:
            self._unprojected(f"item/completed/{item_type}/duplicate")
            return
        if item_type == "commandExecution":
            status = item.get("status")
            if status not in {"completed", "failed", "declined"}:
                self._unprojected("item/completed/commandExecution/status")
                return
            structured: Json = {
                key: value
                for key, value in item.items()
                if key not in {"type", "id", "aggregatedOutput"} and _is_json(value)
            }
            outcome = {
                "completed": ToolOutcome.SUCCEEDED,
                "failed": ToolOutcome.FAILED,
                "declined": ToolOutcome.FAILED,
            }[status]
        else:
            status = item.get("status")
            if status not in {"completed", "failed"}:
                self._unprojected("item/completed/mcpToolCall/status")
                return
            result = item.get("result")
            error = item.get("error")
            if result is not None and not isinstance(result, dict):
                self._unprojected("item/completed/mcpToolCall/result")
                return
            if error is not None and not isinstance(error, dict):
                self._unprojected("item/completed/mcpToolCall/error")
                return
            rendered = _render_mcp_result(result)
            if rendered:
                self.events.append(
                    ItemSegment(
                        item=CallRef(call_id=item_id), text=rendered, provenance=FrameRange(frame_seq, frame_seq)
                    )
                )
            structured = {
                key: value for key, value in item.items() if key not in {"type", "id", "arguments"} and _is_json(value)
            }
            outcome = ToolOutcome.SUCCEEDED if status == "completed" else ToolOutcome.FAILED
        self.completed_call_ids.add(item_id)
        self.events.append(
            ToolCallCompleted(
                item=CallRef(call_id=item_id),
                structured=structured,
                outcome=outcome,
                provenance=FrameRange(frame_seq, frame_seq),
            )
        )

    def _turn_completed(self, frame_seq: int, params: Mapping[str, Any]) -> None:
        turn = params.get("turn") if isinstance(params.get("turn"), dict) else None
        match turn and turn.get("status"):
            case "completed":
                end: TurnEnd = TurnAnswered()
            case "interrupted":
                end = TurnAborted()
            case "failed":
                assert turn is not None
                end = TurnFailed(reason=_failure(turn))
            case _:
                self._unprojected("turn/completed/status")
                return
        self._close_message()
        self._close_reasoning()
        self.events.append(TurnCompleted(end=end, provenance=FrameRange(frame_seq, frame_seq)))
        self.seen_call_ids.clear()
        self.completed_call_ids.clear()

    def _close_message(self) -> None:
        if self.open_message is None:
            return
        opened = self.open_message
        self.open_message = None
        self.events.append(MessageCompleted(backend_item_id=opened.backend_item_id, provenance=_span(opened)))

    def _close_reasoning(self) -> None:
        if self.open_reasoning is None:
            return
        opened = self.open_reasoning
        self.open_reasoning = None
        self.events.append(ReasoningCompleted(disclosure=ReasoningDisclosure.SUMMARY, provenance=_span(opened)))

    def _unprojected(self, kind: str) -> None:
        self.unprojected[kind] = self.unprojected.get(kind, 0) + 1


def _failure(turn: Mapping[str, Any]) -> str:
    """Why the turn failed, from `TurnError.message` on the terminal frame.

    `additionalDetails` holds the reason instead on the retry notifications Codex sends while it is
    still trying, and is null here; see `docs/protocol_evidence.md` § Turn failures.
    """
    error = turn.get("error")
    if error is None:
        return "unknown error"
    if isinstance(error, dict) and isinstance(message := error.get("message"), str):
        return message
    return str(error)


def _item(params: Mapping[str, Any]) -> dict[str, Any] | None:
    value = params.get("item")
    return value if isinstance(value, dict) else None


def _span(item: OpenItem) -> FrameRange:
    return FrameRange(item.opened_at_frame_seq, item.last_frame_seq)


def _undelivered(text: str, delivered: str) -> str:
    """The suffix not already sent as deltas, tolerating a fold resumed mid-item."""
    overlap = min(len(text), len(delivered))
    while overlap and not delivered.endswith(text[:overlap]):
        overlap -= 1
    return text[overlap:]


def _render_mcp_result(result: Mapping[str, Any] | None) -> str:
    if result is None:
        return ""
    content = result.get("content")
    if not isinstance(content, list):
        return ""
    rendered: list[str] = []
    for part in content:
        if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str):
            rendered.append(part["text"])
        elif _is_json(part):
            rendered.append(json.dumps(part, sort_keys=True, separators=(",", ":")))
    return "".join(rendered)


def _is_json(value: Any) -> bool:
    if value is None or isinstance(value, (bool, int, float, str)):
        return True
    if isinstance(value, list):
        return all(_is_json(member) for member in value)
    if isinstance(value, dict):
        return all(isinstance(key, str) and _is_json(member) for key, member in value.items())
    return False
