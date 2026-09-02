"""Claude Code over stream-json: launch, input, interrupt, and frame translation.

Observed with Claude Code 2.1.252 under `--replay-user-messages`:

- a `command_lifecycle` frame with state `queued` follows each accepted user frame at once, keyed by
  the frame's uuid; `started` and `completed` follow when the harness processes it, and the `user`
  echo with `isReplay` comes with `started`;
- streamed blocks arrive as `content_block_start`, deltas, then one `assistant` frame holding the
  completed block, then `content_block_stop`; after a lost stream the retry is non-streaming and
  only the `assistant` frames appear;
- tool results come back as a `user` frame with `tool_result` blocks;
- one `result` frame ends a turn, even when queued inputs joined it.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from x.agentplane.native.claude import driver, scenarios
from x.agentplane.runner import protocol_pb2 as pb
from x.agentplane.runner.adapter import HarnessAdapter
from x.agentplane.runner.config import ClaudeLaunch

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf

if TYPE_CHECKING:
    from x.agentplane.runner.session import Frame, Session

_ITEM_KINDS = {
    "text": pb.ITEM_KIND_ASSISTANT_TEXT,
    "thinking": pb.ITEM_KIND_REASONING,
    "tool_use": pb.ITEM_KIND_TOOL_CALL,
}


class ClaudeAdapter(HarnessAdapter):
    def __init__(self, session: Session, launch: ClaudeLaunch) -> None:
        self.session = session
        self.launch = launch
        self._native_session_id = session.record.native_session_id or str(uuid4())
        # User frame uuid to input id, until the harness reports the frame queued.
        self._pending: dict[str, str] = {}
        self._message_id = ""
        # Content block index to item id for the message being streamed.
        self._block_items: dict[int, str] = {}
        # Blocks already completed per message id, so non-streamed `assistant` frames get the same
        # item ids the stream would have given them.
        self._blocks_completed: dict[str, int] = {}
        self._items: dict[str, int] = {}

    def command(self) -> list[str]:
        resume_id = self.session.record.native_session_id
        return [
            *self.launch.command_prefix,
            *scenarios.command(
                str(self.launch.binary),
                model=self.session.record.model,
                resume_id=resume_id,
                session_id=None if resume_id else self._native_session_id,
                replay_user_messages=True,
                effort=self.session.record.reasoning_effort or None,
            ),
        ]

    def environment(self) -> Mapping[str, str]:
        config_dir = self.session.directory / "claude"
        config_dir.mkdir(exist_ok=True)
        return {
            **self.session.config.environment,
            **scenarios.environment(
                endpoint=self.launch.base_url, token=self.launch.auth_token, config_dir=str(config_dir)
            ),
        }

    async def handshake(self) -> str:
        initialize = driver.initialize()
        await self.session.request(
            initialize, matches=lambda frame: _control_response_for(frame, initialize["request_id"])
        )
        return self._native_session_id

    async def submit(self, input_id: str, text: str) -> None:
        if not self.session.active_turn_id:
            self.session.emit(pb.TurnStarted(turn_id=f"turn-{uuid4().hex}"), sources=[])
        frame = driver.user_frame(text)
        self._pending[str(frame["uuid"])] = input_id
        self.session.emit(pb.InputSubmitted(input_id=input_id), sources=[])
        await self.session.write_native(frame)

    async def interrupt(self) -> None:
        await self.session.write_native(driver.interrupt(cancel_queued=False))

    async def on_frame(self, frame: Frame) -> None:
        match frame.get("type"):
            case "control_request":
                await self._answer_control_request(frame)
            case "command_lifecycle":
                if frame.get("state") == "queued" and (
                    input_id := self._pending.pop(str(frame.get("command_uuid")), None)
                ):
                    self.session.emit(pb.InputAccepted(input_id=input_id, turn_id=self._turn_id()))
            case "stream_event":
                self._on_stream_event(_dict(frame.get("event")))
            case "assistant":
                self._on_assistant(_dict(frame.get("message")))
            case "user":
                self._on_user(_dict(frame.get("message")))
            case "result":
                self._on_result(frame)

    async def _answer_control_request(self, frame: Frame) -> None:
        request = _dict(frame.get("request"))
        request_id = frame.get("request_id", "")
        if request.get("subtype") == "can_use_tool":
            response: dict[str, Any] = {
                "subtype": "success",
                "request_id": request_id,
                "response": {"behavior": "allow", "updatedInput": request.get("input", {})},
            }
        else:
            # Dialogs, hooks, and MCP callbacks have no answer path here; a refusal keeps the turn moving.
            response = {
                "subtype": "error",
                "request_id": request_id,
                "error": f"the agentplane runner does not answer {request.get('subtype')!r} requests",
            }
        await self.session.write_native({"type": "control_response", "response": response})

    def _turn_id(self) -> str:
        """The active turn, or a new one for output the harness produces on its own, such as a
        queued input it chose to run as a fresh turn after the previous result."""
        if not self.session.active_turn_id:
            self.session.emit(pb.TurnStarted(turn_id=f"turn-{uuid4().hex}"))
        return self.session.active_turn_id

    def _on_stream_event(self, event: Frame) -> None:
        match event.get("type"):
            case "message_start":
                self._message_id = str(_dict(event.get("message")).get("id", ""))
                self._block_items = {}
            case "content_block_start":
                block = _dict(event.get("content_block"))
                index = int(event.get("index", 0))
                item_id = self._item_id(block, index)
                self._block_items[index] = item_id
                self._start_item(item_id, block)
            case "content_block_delta":
                delta = _dict(event.get("delta"))
                item_id = self._block_items.get(int(event.get("index", 0)), "")
                match delta.get("type"):
                    case "text_delta":
                        self.session.emit(pb.TextDelta(item_id=item_id, text=str(delta.get("text", ""))))
                    case "thinking_delta":
                        self.session.emit(pb.TextDelta(item_id=item_id, text=str(delta.get("thinking", ""))))
                    case "input_json_delta":
                        self.session.emit(
                            pb.ToolArgumentsDelta(item_id=item_id, partial_json=str(delta.get("partial_json", "")))
                        )

    def _on_assistant(self, message: Frame) -> None:
        message_id = str(message.get("id", ""))
        for block in _blocks(message):
            index = self._blocks_completed.get(message_id, 0)
            self._blocks_completed[message_id] = index + 1
            item_id = self._item_id(block, index, message_id=message_id)
            if item_id not in self._items:
                self._start_item(item_id, block)
            match block.get("type"):
                case "text":
                    self.session.emit(pb.ItemCompleted(item_id=item_id, text=str(block.get("text", ""))))
                case "thinking":
                    self.session.emit(pb.ItemCompleted(item_id=item_id, text=str(block.get("thinking", ""))))
                case "tool_use":
                    self.session.emit(
                        pb.ToolArguments(item_id=item_id, arguments_json=json.dumps(block.get("input", {})))
                    )

    def _on_user(self, message: Frame) -> None:
        for block in _blocks(message):
            if block.get("type") != "tool_result":
                continue
            self.session.emit(
                pb.ItemCompleted(
                    item_id=str(block.get("tool_use_id", "")),
                    tool=pb.ToolResult(
                        output=_result_text(block.get("content")), succeeded=not block.get("is_error", False)
                    ),
                )
            )

    def _on_result(self, frame: Frame) -> None:
        if not self.session.active_turn_id:
            return
        terminal_reason = str(frame.get("terminal_reason", ""))
        if terminal_reason.startswith("aborted"):
            status, error = pb.TURN_STATUS_INTERRUPTED, ""
        elif frame.get("is_error"):
            status, error = pb.TURN_STATUS_FAILED, str(frame.get("result", ""))
        else:
            status, error = pb.TURN_STATUS_COMPLETED, ""
        self.session.emit(pb.TurnCompleted(turn_id=self.session.active_turn_id, status=status, error=error))

    def _item_id(self, block: Frame, index: int, *, message_id: str | None = None) -> str:
        if block.get("type") == "tool_use":
            return str(block.get("id", ""))
        return f"{message_id if message_id is not None else self._message_id}#{index}"

    def _start_item(self, item_id: str, block: Frame) -> None:
        kind = _ITEM_KINDS.get(str(block.get("type")), pb.ITEM_KIND_UNSPECIFIED)
        self._items[item_id] = kind
        self._turn_id()
        self.session.emit(pb.ItemStarted(item_id=item_id, kind=kind, tool_name=str(block.get("name", ""))))


def _control_response_for(frame: Frame, request_id: str) -> bool:
    return frame.get("type") == "control_response" and _dict(frame.get("response")).get("request_id") == request_id


def _dict(value: object) -> Frame:
    return value if isinstance(value, dict) else {}


def _blocks(message: Frame) -> list[Frame]:
    content = message.get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return [block for block in content if isinstance(block, dict)] if isinstance(content, list) else []


def _result_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(block.get("text", "")) for block in content if isinstance(block, dict) and block.get("type") == "text"
        )
    return ""
