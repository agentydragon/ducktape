"""Claude Code over stream-json: launch, input, interrupt, and frame translation.

Observed with Claude Code 2.1.252 under `--replay-user-messages`:

- a `command_lifecycle` frame with state `queued` follows each accepted user frame at once, keyed by
  the frame's uuid; that is queue admission only. `started` identifies the inputs Claude takes into
  a native prompt. For a batch, replay first emits synthetic follower echoes, then `started` for all
  contributors, and finally the representative (last-UUID) user echo whose text is newline-joined;
- when a tool is active, later inputs instead join its continuation request. Claude emits the tool
  result, then `started` for every input in that continuation, but no replayed user text; the
  runner correlates that tool-result frame and started cohort as its confirmation evidence;
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

from x.agentplane.native.claude import driver, facade, scenarios, wire
from x.agentplane.native.claude.blocks import Block, TextBlock, ThinkingBlock, ToolResultBlock, ToolUseBlock, blocks_of
from x.agentplane.protocol import event_pb2
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
        # The facade only performs typed native I/O. Session still logs every receipt before this
        # adapter sees it and this adapter alone projects those frames into runner observations.
        self.harness = facade.ClaudeHarness(session)
        self._native_session_id = session.record.native_session_id or str(uuid4())
        # User frame uuid to command data, until the harness confirms its native message.
        self._pending: dict[str, tuple[str, str]] = {}
        # `started` lifecycle UUIDs awaiting the representative replayed native user message. A
        # Claude batch starts every contributor but emits only its last UUID with the joined text.
        self._started_inputs: list[tuple[str, int]] = []
        # A user frame holding a tool result is followed by lifecycle ``started`` for inputs
        # Claude folds into that still-active turn. There is intentionally no replayed user
        # frame for those inputs, so the lifecycle cohort itself is the native confirmation.
        self._tool_result_message: tuple[str, int] | None = None
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
        # Every start sends the session's standing instructions, so a resumed harness has them too.
        response = await self.harness.initialize(instructions=self.session.record.instructions)
        if not isinstance(response.response, wire.ControlResponseFrame):
            raise RuntimeError(f"Claude initialization failed: {response.response}")
        return self._native_session_id

    async def submit(self, command_id: str, text: str) -> None:
        if not self.session.active_turn_id:
            self.session.emit(
                event_pb2.TurnStarted(turn_id=f"turn-{uuid4().hex}", model=self.session.record.model), sources=[]
            )
        frame = await self.harness.submit(text)
        self._pending[frame.uuid] = (command_id, text)

    async def interrupt(self) -> None:
        # Claude accepts this immediately, but its control acknowledgement is not the command's
        # effect: the translated terminal result is what releases the command.
        await self.harness.signal_interrupt(cancel_queued=False, reason="agentplane")

    async def change_model(self, command_id: str, model: str) -> None:
        receipt = await self.harness.set_model(model)
        response = receipt.response
        if not isinstance(response, wire.ControlResponseFrame) or response.response.subtype != "success":
            detail = response.response.error if isinstance(response, wire.ControlResponseFrame) else "invalid response"
            raise RuntimeError(f"Claude Code refused model switch: {detail}")
        self.session.model_changed(command_id, model, sources=[receipt.sequence])

    async def on_frame(self, frame: Frame, source_sequence: int) -> None:
        parsed = wire.parse_frame(frame)
        if (
            self._tool_result_message is not None
            and not (isinstance(parsed, wire.CommandLifecycleFrame) and parsed.state is wire.CommandState.STARTED)
            and not (
                # With replay enabled, a coalesced active-turn batch first emits synthetic follower
                # echo(es). They are queue bookkeeping, not a new native prompt and not the end of
                # the tool-result continuation window; the following started cohort establishes it.
                isinstance(parsed, wire.UserFrame) and parsed.is_replay and parsed.uuid in self._pending
            )
        ):
            await self._confirm_tool_result_inputs()
        match parsed:
            case wire.ControlRequestFrame() as request:
                await self._answer_control_request(request)
            case wire.CommandLifecycleFrame(state=wire.CommandState.STARTED, command_uuid=command_uuid):
                if command_uuid in self._pending:
                    self._started_inputs.append((command_uuid, source_sequence))
            case wire.CommandLifecycleFrame(state=wire.CommandState.CANCELLED, command_uuid=command_uuid):
                if pending := self._pending.pop(command_uuid, None):
                    command_id, _ = pending
                    self._started_inputs = [item for item in self._started_inputs if item[0] != command_uuid]
                    self.session._noop(command_id, "Claude cancelled the queued user input before taking it")
            case wire.StreamEventFrame() as streamed:
                await self._on_stream_event(streamed, source_sequence)
            case wire.AssistantFrame(message=message):
                self._on_assistant(message)
            case wire.UserFrame() as user:
                await self._on_user(user, source_sequence)
            case wire.ResultFrame() as result:
                await self._on_result(result)

    async def _answer_control_request(self, frame: wire.ControlRequestFrame) -> None:
        match frame.request:
            case wire.CanUseTool(input=tool_input):
                # TODO: the runner's permission configuration is meant to keep these prompts from
                # arriving at all; answering allow is a stopgap to revisit if one is ever observed.
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
        await self.harness.send(response)

    def _turn_id(self) -> str:
        """The active turn, or a new one for output the harness produces on its own, such as a
        queued input it chose to run as a fresh turn after the previous result."""
        if not self.session.active_turn_id:
            self.session.emit(event_pb2.TurnStarted(turn_id=f"turn-{uuid4().hex}", model=self.session.record.model))
        return self.session.active_turn_id

    async def _on_stream_event(self, frame: wire.StreamEventFrame, source_sequence: int) -> None:
        event = frame.event
        match event:
            case wire.MessageStart(message=message):
                self._message_id = message.id
                self._block_items = {}
                if frame.user_message_uuid:
                    await self._confirm_message_start(frame.user_message_uuid, source_sequence)
            case wire.ContentBlockStart(index=index, content_block=block):
                item_id = self._item_id(block, index, self._message_id)
                self._block_items[index] = item_id
                self._start_item(item_id, block)
            case wire.ContentBlockDelta(index=index, delta=delta):
                item_id = self._block_items.get(index, "")
                match delta:
                    case wire.TextDelta(text=text) | wire.ThinkingDelta(thinking=text):
                        self.session.emit(event_pb2.TextDelta(item_id=item_id, text=text))
                    case wire.InputJsonDelta(partial_json=partial_json):
                        self.session.emit(event_pb2.ToolArgumentsDelta(item_id=item_id, partial_json=partial_json))

    async def _confirm_message_start(self, harness_message_id: str, source_sequence: int) -> None:
        """Confirm the native prompt which caused one Claude model request.

        Claude's replay flag supplies a textual echo for a coalesced queue batch but not for its
        ordinary one-message path. ``stream_event.message_start.user_message_uuid`` is present in
        both of those model-request cases and is the durable correlation that the prompt began.
        """
        started_uuids = [uuid for uuid, _ in self._started_inputs]
        if harness_message_id not in started_uuids:
            return
        if harness_message_id != started_uuids[-1]:
            raise RuntimeError("Claude started a model request for a non-representative input")
        contributors = list(self._started_inputs)
        pending = [self._pending.get(uuid) for uuid, _ in contributors]
        if any(item is None for item in pending):
            raise RuntimeError("Claude started an input the runner did not have pending")
        confirmed = [item for item in pending if item is not None]
        for uuid, _ in contributors:
            del self._pending[uuid]
        self._started_inputs = []
        await self.session.confirm_user_message(
            harness_message_id=harness_message_id,
            text="\n".join(item[1] for item in confirmed),
            origin_command_ids=[item[0] for item in confirmed],
            turn_id=self._turn_id(),
            sources=[*(sequence for _, sequence in contributors), source_sequence],
        )

    def _on_assistant(self, message: wire.AssistantMessage) -> None:
        for block in message.content:
            index = self._blocks_completed.get(message.id, 0)
            self._blocks_completed[message.id] = index + 1
            item_id = self._item_id(block, index, message.id)
            if item_id not in self._items:
                self._start_item(item_id, block)
            match block:
                case TextBlock(text=text) | ThinkingBlock(thinking=text):
                    self.session.emit(event_pb2.ItemCompleted(item_id=item_id, text=text))
                case ToolUseBlock(input=tool_input):
                    self.session.emit(event_pb2.ToolArguments(item_id=item_id, arguments_json=json.dumps(tool_input)))

    async def _on_user(self, frame: wire.UserFrame, source_sequence: int) -> None:
        if frame.is_replay:
            await self._confirm_replayed_user_message(frame, source_sequence)
            return
        message = frame.message
        blocks = list(blocks_of(message.content))
        for block in blocks:
            if isinstance(block, ToolResultBlock):
                self.session.emit(
                    event_pb2.ItemCompleted(
                        item_id=block.tool_use_id,
                        tool=event_pb2.ToolResult(output=block.text, succeeded=not block.is_error),
                    )
                )
        if any(isinstance(block, ToolResultBlock) for block in blocks):
            self._tool_result_message = (frame.uuid, source_sequence)

    async def _confirm_replayed_user_message(self, frame: wire.UserFrame, source_sequence: int) -> None:
        """Confirm exactly one native Claude prompt, rather than its replay bookkeeping.

        `--replay-user-messages` outputs each follower of a coalesced batch once in its original
        form before the lifecycle `started` frames. Those echoes never reached the model as their
        own messages. The later representative echo has the last UUID and joined text; the started
        cohort provides its exact command origins without trying to reconstruct them from newlines.
        """
        started_uuids = [uuid for uuid, _ in self._started_inputs]
        if frame.uuid not in started_uuids:
            return
        if frame.uuid != started_uuids[-1]:
            raise RuntimeError("Claude replayed a non-representative started input")
        contributors = list(self._started_inputs)
        pending = [self._pending.get(uuid) for uuid, _ in contributors]
        if any(item is None for item in pending):
            raise RuntimeError("Claude started an input the runner did not have pending")
        confirmed = [item for item in pending if item is not None]
        text = "\n".join(item[1] for item in confirmed)
        if frame.message.content != text:
            raise RuntimeError("Claude replayed user text inconsistent with its started input batch")
        for uuid, _ in contributors:
            del self._pending[uuid]
        self._started_inputs = []
        await self.session.confirm_user_message(
            harness_message_id=frame.uuid,
            text=text,
            origin_command_ids=[item[0] for item in confirmed],
            turn_id=self._turn_id(),
            sources=[*(sequence for _, sequence in contributors), source_sequence],
        )

    async def _confirm_tool_result_inputs(self) -> None:
        """Confirm inputs Claude begins after a tool result, without inventing a user echo.

        Native Claude puts an active-turn input into the continuation request alongside the tool
        result, then reports ``started`` once per input. The tool-result frame does not contain
        the input text and no replayed user frame follows, so its UUID plus that exact lifecycle
        cohort is the strongest native causal evidence available.
        """
        tool_result_message = self._tool_result_message
        self._tool_result_message = None
        if tool_result_message is None or not self._started_inputs:
            return
        contributors = list(self._started_inputs)
        pending = [self._pending.get(uuid) for uuid, _ in contributors]
        if any(item is None for item in pending):
            raise RuntimeError("Claude started an input the runner did not have pending")
        confirmed = [item for item in pending if item is not None]
        for uuid, _ in contributors:
            del self._pending[uuid]
        self._started_inputs = []
        harness_message_id, tool_result_sequence = tool_result_message
        await self.session.confirm_user_message(
            harness_message_id=harness_message_id,
            text="\n".join(item[1] for item in confirmed),
            origin_command_ids=[item[0] for item in confirmed],
            turn_id=self._turn_id(),
            sources=[tool_result_sequence, *(sequence for _, sequence in contributors)],
        )

    async def _on_result(self, result: wire.ResultFrame) -> None:
        if not self.session.active_turn_id:
            return
        if (result.terminal_reason or "").startswith("aborted"):
            status, error = event_pb2.TURN_STATUS_INTERRUPTED, ""
        elif result.is_error:
            status, error = event_pb2.TURN_STATUS_FAILED, result.result or ""
        else:
            status, error = event_pb2.TURN_STATUS_COMPLETED, ""
        await self.session.turn_completed(self.session.active_turn_id, status, error)

    @staticmethod
    def _item_id(block: Block, index: int, message_id: str) -> str:
        return block.id if isinstance(block, ToolUseBlock) else f"{message_id}#{index}"

    def _start_item(self, item_id: str, block: Block) -> None:
        match block:
            case TextBlock():
                kind, tool_name = event_pb2.ITEM_KIND_ASSISTANT_TEXT, ""
            case ThinkingBlock():
                kind, tool_name = event_pb2.ITEM_KIND_REASONING, ""
            case ToolUseBlock(name=name):
                kind, tool_name = event_pb2.ITEM_KIND_TOOL_CALL, name
            case _:
                # Tool results complete items rather than start them; unknown block kinds stay
                # native evidence only.
                return
        self._items.add(item_id)
        self._turn_id()
        self.session.emit(event_pb2.ItemStarted(item_id=item_id, kind=kind, tool_name=tool_name))
