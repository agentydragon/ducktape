"""Codex app-server notifications into neutral conversation operations, runner-side.

The Codex half of the #4667 boundary: the runner interprets the app-server's own JSON-RPC-shaped
notifications and emits the materialized operations of <../neutral_operations.py>, so the Console stops
parsing native payloads. Semantics are ported from the Console projector this replaces at the
generation cut (`haku/console/x/codex_app_server/projection.py`), written against what the wire does
rather than what it documents.

**Turns are brackets the runner draws.** Codex never wakes itself and never opens a turn on its own
stream, so — unlike Claude — a turn opens only when a prompt is admitted (`admit`, `PromptsCause`),
and `turn/completed` closes it. Every item the app-server streams inside that bracket belongs to the
open turn.

**Identity is minted here.** Every turn and item travels under a runner-minted `UUID` that every
later operation repeats; Codex's own item ids (`item.id`) ride along as `backend_item_id` provenance
and key the runner ids while an item is open. Codex-specific item classes (file changes, plans, web
search, and newer additions) are counted in `Projected.unprojected` rather than crashed on or
promoted into a competing vocabulary.

Only facts the neutral vocabulary already carries are projected: `agentMessage` (message
lifecycle/text), `reasoning` summary (reasoning lifecycle/summary, `disclosure=summary`),
`commandExecution`/`mcpToolCall` (tool-call lifecycles), and `turn/completed` (the terminal
outcome). Notifications the design assigns elsewhere — token usage, thread status, approval
progress, MCP progress narration — are ignored explicitly.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any
from uuid import UUID, uuid4

from haku.runner.codex.protocol import TURN_COMPLETED, Notification, Request, Response, UnknownMessage, parse_message
from haku.runner.neutral_operations import (
    FrameRange,
    ItemCompleted,
    ItemOpened,
    ItemSegment,
    Json,
    MessageCompletion,
    MessageOpen,
    PromptAdmitted,
    PromptsCause,
    ReasoningCompletion,
    ReasoningDisclosure,
    ReasoningOpen,
    ToolCallCompletion,
    ToolCallOpen,
    ToolOutcome,
    TurnAborted,
    TurnAnswered,
    TurnEnd,
    TurnEnded,
    TurnFailed,
    TurnOpened,
)
from haku.runner.projection import Projected, Yield, at, undelivered

# Notifications the neutral vocabulary deliberately does not carry, listed so a new one lands in the
# default branch (counted) instead of here (dropped): thread lifecycle, token accounting, the
# request-side turn open, reasoning part boundaries, MCP progress, and server-request resolution.
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


@dataclass(frozen=True, slots=True)
class _OpenItem:
    """A Codex item seen starting but not ending: its runner id and how far its prose has been sent."""

    item_id: UUID
    backend_item_id: str
    opened_at_frame_seq: int
    last_frame_seq: int
    delivered: str = ""


def _item(params: Mapping[str, Any]) -> dict[str, Any] | None:
    value = params.get("item")
    return value if isinstance(value, dict) else None


def _span(item: _OpenItem) -> FrameRange:
    return FrameRange(first_frame_seq=item.opened_at_frame_seq, last_frame_seq=item.last_frame_seq)


class CodexProjector:
    """The stateful fold from one Codex app-server's stream to neutral operations.

    Feed every stdout notification to `observe` in the runner's numbering order; report every
    admitted prompt to `admit` at the injection fence. Both return the operations to journal, in
    order, and never raise on wire content: an unreadable notification is counted, not fatal.
    """

    def __init__(self, mint_id: Any = uuid4) -> None:
        self._mint_id = mint_id
        self._open_turn: UUID | None = None
        self._open_message: _OpenItem | None = None
        self._open_reasoning: _OpenItem | None = None
        # Runner item ids for calls declared but unanswered, keyed by Codex's item id; settled ids
        # stay known so a repeated completion is recognized rather than re-opened.
        self._open_calls: dict[str, UUID] = {}
        self._completed_call_ids: set[str] = set()

    def admit(self, prompt_id: UUID, *, after_batch_seq: int | None, frame_seq: int | None = None) -> Projected:
        """The runner injected Console prompt *prompt_id* as Codex's `turn/start` at this fence.

        Idle — which for Codex is always, since it opens no turn of its own — the admission opens
        the turn it causes. A `turn/start` only lands with no turn open, so there is no in-turn
        fence-only case Claude needs.
        """
        provenance = at(frame_seq) if frame_seq is not None else None
        result = Yield()
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
        """Fold one app-server stdout message, numbered *frame_seq* by the runner."""
        result = Yield()
        message = parse_message(payload)
        if isinstance(message, (Request, Response)):
            # A local request's answer or a refused server request: recorded on the wire, but the
            # conversation is the notification stream.
            return result.projected()
        if isinstance(message, UnknownMessage):
            result.miss(message.reason)
            return result.projected()
        assert isinstance(message, Notification)
        if message.method in _IGNORED_METHODS:
            return result.projected()
        if (params := message.params) is None:
            result.miss(f"{message.method}/params")
            return result.projected()
        match message.method:
            case "item/started":
                self._item_started(result, frame_seq, params)
            case "item/agentMessage/delta":
                self._message_delta(result, frame_seq, params)
            case "item/reasoning/summaryTextDelta":
                self._reasoning_delta(result, frame_seq, params)
            case "item/commandExecution/outputDelta":
                self._command_delta(result, frame_seq, params)
            case "item/completed":
                self._item_completed(result, frame_seq, params)
            case _ if message.method == TURN_COMPLETED:
                self._turn_completed(result, frame_seq, params)
            case _:
                result.miss(message.method)
        return result.projected()

    def _item_started(self, result: Yield, frame_seq: int, params: Mapping[str, Any]) -> None:
        item = _item(params)
        if item is None:
            result.miss("item/started/item")
            return
        item_type, item_id = item.get("type"), item.get("id")
        if not isinstance(item_type, str) or not isinstance(item_id, str):
            result.miss("item/started/identity")
            return
        if item_type == "agentMessage":
            self._open_message = self._open_prose(result, self._open_message, MessageOpen(), frame_seq, item_id)
        elif item_type == "reasoning":
            self._open_reasoning = self._open_prose(result, self._open_reasoning, ReasoningOpen(), frame_seq, item_id)
        elif item_type in ("commandExecution", "mcpToolCall"):
            self._start_tool(result, item, frame_seq)
        elif item_type == "userMessage":
            # The request-side prompt, authored by the Console before a backend frame claims it.
            pass
        else:
            result.miss(f"item/started/{item_type}")

    def _open_prose(
        self,
        result: Yield,
        open_item: _OpenItem | None,
        item: MessageOpen | ReasoningOpen,
        frame_seq: int,
        item_id: str,
    ) -> _OpenItem:
        """Open one prose item (message or reasoning), closing a different open one of its kind."""
        if open_item is not None and open_item.backend_item_id != item_id:
            self._close_prose(result, open_item, item)
            open_item = None
        if open_item is not None:
            return replace(open_item, last_frame_seq=frame_seq)
        runner_id = self._mint_id()
        result.operations.append(
            ItemOpened(
                item_id=runner_id, turn_id=self._open_turn, item=item, backend_item_id=item_id, provenance=at(frame_seq)
            )
        )
        return _OpenItem(
            item_id=runner_id, backend_item_id=item_id, opened_at_frame_seq=frame_seq, last_frame_seq=frame_seq
        )

    def _message_delta(self, result: Yield, frame_seq: int, params: Mapping[str, Any]) -> None:
        self._open_message = self._prose_delta(result, self._open_message, MessageOpen(), frame_seq, params)

    def _reasoning_delta(self, result: Yield, frame_seq: int, params: Mapping[str, Any]) -> None:
        self._open_reasoning = self._prose_delta(result, self._open_reasoning, ReasoningOpen(), frame_seq, params)

    def _prose_delta(
        self,
        result: Yield,
        open_item: _OpenItem | None,
        item: MessageOpen | ReasoningOpen,
        frame_seq: int,
        params: Mapping[str, Any],
    ) -> _OpenItem | None:
        item_id, delta = params.get("itemId"), params.get("delta")
        kind = "agentMessage" if isinstance(item, MessageOpen) else "reasoning"
        if not isinstance(item_id, str) or not isinstance(delta, str):
            result.miss(f"item/{kind}/delta/shape")
            return open_item
        if open_item is None:
            open_item = self._open_prose(result, None, item, frame_seq, item_id)
        if open_item.backend_item_id != item_id:
            result.miss(f"item/{kind}/delta/itemId")
            return open_item
        if delta:
            result.operations.append(ItemSegment(item_id=open_item.item_id, text=delta, provenance=at(frame_seq)))
        return replace(open_item, last_frame_seq=frame_seq, delivered=open_item.delivered + delta)

    def _command_delta(self, result: Yield, frame_seq: int, params: Mapping[str, Any]) -> None:
        item_id, delta = params.get("itemId"), params.get("delta")
        if not isinstance(item_id, str) or not isinstance(delta, str):
            result.miss("item/commandExecution/outputDelta/shape")
            return
        if (runner_id := self._open_calls.get(item_id)) is None:
            result.miss("item/commandExecution/outputDelta/itemId")
            return
        if delta:
            result.operations.append(ItemSegment(item_id=runner_id, text=delta, provenance=at(frame_seq)))

    def _item_completed(self, result: Yield, frame_seq: int, params: Mapping[str, Any]) -> None:
        item = _item(params)
        if item is None:
            result.miss("item/completed/item")
            return
        item_type, item_id = item.get("type"), item.get("id")
        if not isinstance(item_type, str) or not isinstance(item_id, str):
            result.miss("item/completed/identity")
            return
        if item_type == "agentMessage":
            self._complete_message(result, item, item_id, frame_seq)
        elif item_type == "reasoning":
            self._complete_reasoning(result, item, item_id, frame_seq)
        elif item_type in ("commandExecution", "mcpToolCall"):
            if item_id not in self._open_calls and item_id not in self._completed_call_ids:
                self._start_tool(result, item, frame_seq)
            self._complete_tool(result, item, item_id, frame_seq)
        elif item_type == "userMessage":
            pass
        else:
            result.miss(f"item/completed/{item_type}")

    def _complete_message(self, result: Yield, item: Mapping[str, Any], item_id: str, frame_seq: int) -> None:
        text = item.get("text")
        if not isinstance(text, str):
            result.miss("item/completed/agentMessage/text")
            return
        self._complete_prose(result, self._open_message, MessageOpen(), MessageCompletion(), item_id, text, frame_seq)
        self._open_message = None

    def _complete_reasoning(self, result: Yield, item: Mapping[str, Any], item_id: str, frame_seq: int) -> None:
        summary = item.get("summary")
        if not isinstance(summary, list) or not all(isinstance(part, str) for part in summary):
            result.miss("item/completed/reasoning/summary")
            return
        completion = ReasoningCompletion(disclosure=ReasoningDisclosure.SUMMARY)
        self._complete_prose(
            result, self._open_reasoning, ReasoningOpen(), completion, item_id, "\n\n".join(summary), frame_seq
        )
        self._open_reasoning = None

    def _complete_prose(
        self,
        result: Yield,
        open_item: _OpenItem | None,
        opener: MessageOpen | ReasoningOpen,
        completion: MessageCompletion | ReasoningCompletion,
        item_id: str,
        text: str,
        frame_seq: int,
    ) -> None:
        if open_item is None or open_item.backend_item_id != item_id:
            if open_item is not None:
                self._close_prose(result, open_item, opener)
            open_item = self._open_prose(result, None, opener, frame_seq, item_id)
        suffix = undelivered(text, open_item.delivered)
        if suffix:
            result.operations.append(ItemSegment(item_id=open_item.item_id, text=suffix, provenance=at(frame_seq)))
        open_item = replace(open_item, last_frame_seq=frame_seq, delivered=open_item.delivered + suffix)
        result.operations.append(
            ItemCompleted(
                item_id=open_item.item_id, completion=completion, backend_item_id=item_id, provenance=_span(open_item)
            )
        )

    def _close_prose(self, result: Yield, open_item: _OpenItem, completion: MessageOpen | ReasoningOpen) -> None:
        """Complete a prose item interrupted by a different one of its kind, on the frames it spanned."""
        done = (
            MessageCompletion()
            if isinstance(completion, MessageOpen)
            else ReasoningCompletion(disclosure=ReasoningDisclosure.SUMMARY)
        )
        result.operations.append(
            ItemCompleted(
                item_id=open_item.item_id,
                completion=done,
                backend_item_id=open_item.backend_item_id,
                provenance=_span(open_item),
            )
        )

    def _start_tool(self, result: Yield, item: Mapping[str, Any], frame_seq: int) -> None:
        item_type, item_id = item.get("type"), item.get("id")
        assert isinstance(item_type, str)
        assert isinstance(item_id, str)
        if item_id in self._open_calls:
            return
        if item_type == "commandExecution":
            command, cwd = item.get("command"), item.get("cwd")
            if not isinstance(command, str) or not isinstance(cwd, str):
                result.miss("item/started/commandExecution/shape")
                return
            tool_name = "commandExecution"
            arguments: dict[str, Json] = {"command": command, "cwd": cwd}
        else:
            server, tool, argument_value = item.get("server"), item.get("tool"), item.get("arguments")
            if not isinstance(server, str) or not isinstance(tool, str) or not isinstance(argument_value, dict):
                result.miss("item/started/mcpToolCall/shape")
                return
            tool_name = f"{server}/{tool}"
            arguments = argument_value
        runner_id = self._mint_id()
        self._open_calls[item_id] = runner_id
        result.operations.append(
            ItemOpened(
                item_id=runner_id,
                turn_id=self._open_turn,
                item=ToolCallOpen(tool_name=tool_name, arguments=arguments),
                backend_item_id=item_id,
                provenance=at(frame_seq),
            )
        )

    def _complete_tool(self, result: Yield, item: Mapping[str, Any], item_id: str, frame_seq: int) -> None:
        if (runner_id := self._open_calls.get(item_id)) is None:
            return
        item_type = item.get("type")
        if item_id in self._completed_call_ids:
            result.miss(f"item/completed/{item_type}/duplicate")
            return
        if item_type == "commandExecution":
            status = item.get("status")
            if status not in ("completed", "failed", "declined"):
                result.miss("item/completed/commandExecution/status")
                return
            structured: Json = {
                key: value
                for key, value in item.items()
                if key not in ("type", "id", "aggregatedOutput") and _is_json(value)
            }
            outcome = ToolOutcome.SUCCEEDED if status == "completed" else ToolOutcome.FAILED
        else:
            status = item.get("status")
            if status not in ("completed", "failed"):
                result.miss("item/completed/mcpToolCall/status")
                return
            error, mcp_result = item.get("error"), item.get("result")
            if (mcp_result is not None and not isinstance(mcp_result, dict)) or (
                error is not None and not isinstance(error, dict)
            ):
                result.miss("item/completed/mcpToolCall/result")
                return
            if rendered := _render_mcp_result(mcp_result):
                result.operations.append(ItemSegment(item_id=runner_id, text=rendered, provenance=at(frame_seq)))
            structured = {
                key: value for key, value in item.items() if key not in ("type", "id", "arguments") and _is_json(value)
            }
            outcome = ToolOutcome.SUCCEEDED if status == "completed" else ToolOutcome.FAILED
        self._completed_call_ids.add(item_id)
        del self._open_calls[item_id]
        result.operations.append(
            ItemCompleted(
                item_id=runner_id,
                completion=ToolCallCompletion(outcome=outcome, structured=structured),
                backend_item_id=item_id,
                provenance=at(frame_seq),
            )
        )

    def _turn_completed(self, result: Yield, frame_seq: int, params: Mapping[str, Any]) -> None:
        turn = params.get("turn") if isinstance(params.get("turn"), dict) else None
        match turn and turn.get("status"):
            case "completed":
                end: TurnEnd = TurnAnswered()
            case "interrupted":
                end = TurnAborted()
            case "failed":
                assert turn is not None
                end = TurnFailed(failure=_failure(turn))
            case _:
                result.miss("turn/completed/status")
                return
        self._close_open_prose(result)
        if (turn_id := self._open_turn) is not None:
            self._open_turn = None
            result.operations.append(TurnEnded(turn_id=turn_id, end=end, provenance=at(frame_seq)))
        else:
            result.miss("turn/completed/no_open_turn")
        self._open_calls.clear()
        self._completed_call_ids.clear()

    def _close_open_prose(self, result: Yield) -> None:
        if self._open_message is not None:
            self._close_prose(result, self._open_message, MessageOpen())
            self._open_message = None
        if self._open_reasoning is not None:
            self._close_prose(result, self._open_reasoning, ReasoningOpen())
            self._open_reasoning = None


def _failure(turn: Mapping[str, Any]) -> str:
    """Why the turn failed, from `TurnError.message` on the terminal frame.

    `additionalDetails` holds the reason instead on the retry notifications Codex sends while it is
    still trying, and is null here; the terminal frame's `message` carries the reason.
    """
    error = turn.get("error")
    if error is None:
        return "unknown error"
    if isinstance(error, dict) and isinstance(message := error.get("message"), str):
        return message
    return str(error)


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
