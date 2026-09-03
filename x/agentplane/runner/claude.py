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
from typing import TYPE_CHECKING
from uuid import uuid4

from x.agentplane.native.claude import driver, scenarios, wire
from x.agentplane.native.claude.blocks import Block, TextBlock, ThinkingBlock, ToolResultBlock, ToolUseBlock, blocks_of
from x.agentplane.runner import protocol_pb2 as pb
from x.agentplane.runner.adapter import HarnessAdapter
from x.agentplane.runner.config import ClaudeLaunch

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf

if TYPE_CHECKING:
    from x.agentplane.runner.session import Frame, Session


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
        self._items: set[str] = set()

    def command(self) -> list[str]:
        resume_id = self.session.record.native_session_id
        return [
            *scenarios.command(
                str(self.launch.binary),
                model=self.session.record.model,
                resume_id=resume_id,
                session_id=None if resume_id else self._native_session_id,
                replay_user_messages=True,
                effort=self.session.record.reasoning_effort or None,
            )
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
            initialize, matches=lambda frame: _control_response_for(frame, initialize.request_id)
        )
        return self._native_session_id

    async def submit(self, input_id: str, text: str) -> None:
        if not self.session.active_turn_id:
            self.session.emit(pb.TurnStarted(turn_id=f"turn-{uuid4().hex}"), sources=[])
        frame = driver.user_frame(text)
        self._pending[frame.uuid] = input_id
        self.session.emit(pb.InputSubmitted(input_id=input_id, text=text), sources=[])
        await self.session.write_native(frame)

    async def interrupt(self) -> None:
        await self.session.write_native(driver.interrupt(cancel_queued=False, reason="agentplane"))

    async def on_frame(self, frame: Frame) -> None:
        match wire.parse_frame(frame):
            case wire.ControlRequestFrame() as request:
                await self._answer_control_request(request)
            case wire.CommandLifecycleFrame(state=wire.CommandState.QUEUED, command_uuid=command_uuid):
                if input_id := self._pending.pop(command_uuid, None):
                    self.session.emit(pb.InputAccepted(input_id=input_id, turn_id=self._turn_id()))
            case wire.StreamEventFrame(event=event):
                self._on_stream_event(event)
            case wire.AssistantFrame(message=message):
                self._on_assistant(message)
            case wire.UserFrame(message=message):
                self._on_user(message)
            case wire.ResultFrame() as result:
                self._on_result(result)

    async def _answer_control_request(self, frame: wire.ControlRequestFrame) -> None:
        match frame.request:
            case wire.CanUseTool(input=tool_input):
                response = driver.allow_tool(frame.request_id, tool_input)
            case wire.HookCallback(subtype=subtype) | wire.UnknownControlRequest(subtype=subtype):
                # Dialogs, hooks, and MCP callbacks have no answer path here; a refusal keeps the turn moving.
                response = wire.ControlResponse(
                    response=wire.ControlResponseBody(
                        subtype="error",
                        request_id=frame.request_id,
                        error=f"the agentplane runner does not answer {subtype!r} requests",
                    )
                )
        await self.session.write_native(response)

    def _turn_id(self) -> str:
        """The active turn, or a new one for output the harness produces on its own, such as a
        queued input it chose to run as a fresh turn after the previous result."""
        if not self.session.active_turn_id:
            self.session.emit(pb.TurnStarted(turn_id=f"turn-{uuid4().hex}"))
        return self.session.active_turn_id

    def _on_stream_event(self, event: wire.StreamEvent) -> None:
        match event:
            case wire.MessageStart(message=message):
                self._message_id = message.id
                self._block_items = {}
            case wire.ContentBlockStart(index=index, content_block=block):
                item_id = self._item_id(block, index, self._message_id)
                self._block_items[index] = item_id
                self._start_item(item_id, block)
            case wire.ContentBlockDelta(index=index, delta=delta):
                item_id = self._block_items.get(index, "")
                match delta:
                    case wire.TextDelta(text=text) | wire.ThinkingDelta(thinking=text):
                        self.session.emit(pb.TextDelta(item_id=item_id, text=text))
                    case wire.InputJsonDelta(partial_json=partial_json):
                        self.session.emit(pb.ToolArgumentsDelta(item_id=item_id, partial_json=partial_json))

    def _on_assistant(self, message: wire.AssistantMessage) -> None:
        for block in message.content:
            index = self._blocks_completed.get(message.id, 0)
            self._blocks_completed[message.id] = index + 1
            item_id = self._item_id(block, index, message.id)
            if item_id not in self._items:
                self._start_item(item_id, block)
            match block:
                case TextBlock(text=text) | ThinkingBlock(thinking=text):
                    self.session.emit(pb.ItemCompleted(item_id=item_id, text=text))
                case ToolUseBlock(input=tool_input):
                    self.session.emit(pb.ToolArguments(item_id=item_id, arguments_json=json.dumps(tool_input)))

    def _on_user(self, message: wire.UserMessage) -> None:
        for block in blocks_of(message.content):
            if isinstance(block, ToolResultBlock):
                self.session.emit(
                    pb.ItemCompleted(
                        item_id=block.tool_use_id, tool=pb.ToolResult(output=block.text, succeeded=not block.is_error)
                    )
                )

    def _on_result(self, result: wire.ResultFrame) -> None:
        if not self.session.active_turn_id:
            return
        if (result.terminal_reason or "").startswith("aborted"):
            status, error = pb.TURN_STATUS_INTERRUPTED, ""
        elif result.is_error:
            status, error = pb.TURN_STATUS_FAILED, result.result or ""
        else:
            status, error = pb.TURN_STATUS_COMPLETED, ""
        self.session.emit(pb.TurnCompleted(turn_id=self.session.active_turn_id, status=status, error=error))

    @staticmethod
    def _item_id(block: Block, index: int, message_id: str) -> str:
        return block.id if isinstance(block, ToolUseBlock) else f"{message_id}#{index}"

    def _start_item(self, item_id: str, block: Block) -> None:
        match block:
            case TextBlock():
                kind, tool_name = pb.ITEM_KIND_ASSISTANT_TEXT, ""
            case ThinkingBlock():
                kind, tool_name = pb.ITEM_KIND_REASONING, ""
            case ToolUseBlock(name=name):
                kind, tool_name = pb.ITEM_KIND_TOOL_CALL, name
            case _:
                # Tool results complete items rather than start them; unknown block kinds stay
                # native evidence only.
                return
        self._items.add(item_id)
        self._turn_id()
        self.session.emit(pb.ItemStarted(item_id=item_id, kind=kind, tool_name=tool_name))


def _control_response_for(frame: Frame, request_id: str) -> bool:
    response = frame.get("response")
    return (
        frame.get("type") == "control_response"
        and isinstance(response, dict)
        and response.get("request_id") == request_id
    )
