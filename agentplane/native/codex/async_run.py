"""A test-only Codex app-server run with typed, independent native event cursors."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from agentplane.native.async_process import AsyncNativeProcess, FrameCursor, FrameResponder
from agentplane.native.codex import facade, wire


class CodexEvents:
    """One cursor over a Codex run's append-only native event trace."""

    def __init__(self, frames: FrameCursor):
        self._frames = frames

    async def next(self) -> wire.CodexFrame:
        return wire.parse_frame(await self._frames.next())

    async def response(self, request_id: wire.RequestId) -> wire.Response:
        while True:
            match await self.next():
                case wire.Response(id=received) as frame:
                    if received == request_id:
                        return frame

    async def turn_started(self, turn_id: str) -> wire.TurnStarted:
        while True:
            match await self.next():
                case wire.TurnStarted(params=wire.TurnParams(turn=wire.Turn(id=received))) as frame:
                    if received == turn_id:
                        return frame

    async def turn_completed(self, turn_id: str) -> wire.TurnCompleted:
        while True:
            match await self.next():
                case wire.TurnCompleted(params=wire.TurnParams(turn=wire.Turn(id=received))) as frame:
                    if received == turn_id:
                        return frame

    async def command_started(self, turn_id: str) -> wire.ItemStarted:
        while True:
            match await self.next():
                case (
                    wire.ItemStarted(
                        params=wire.ItemParams(turn_id=received_turn, item=wire.CommandExecutionItem())
                    ) as frame
                ):
                    if received_turn == turn_id:
                        return frame

    async def agent_message_delta(self, turn_id: str) -> wire.AgentMessageDelta:
        while True:
            match await self.next():
                case wire.AgentMessageDelta(params=wire.ItemDeltaParams(turn_id=received)) as frame:
                    if received == turn_id:
                        return frame

    async def error(self, turn_id: str) -> wire.ErrorNotification:
        while True:
            match await self.next():
                case wire.ErrorNotification(params=wire.ErrorParams(turn_id=received)) as frame:
                    if received == turn_id:
                        return frame


class CodexTurn:
    """A started Codex turn and the native events emitted after its start request."""

    def __init__(self, thread_id: str, turn_id: str, events: CodexEvents):
        self.thread_id = thread_id
        self.id = turn_id
        self._events = events

    async def started(self) -> wire.TurnStarted:
        return await self._events.turn_started(self.id)

    async def completed(self) -> wire.TurnCompleted:
        return await self._events.turn_completed(self.id)

    async def agent_message_delta(self) -> wire.AgentMessageDelta:
        return await self._events.agent_message_delta(self.id)

    async def error(self) -> wire.ErrorNotification:
        return await self._events.error(self.id)


class CodexRun:
    """One native Codex app-server process, initialized around one started or resumed thread."""

    def __init__(
        self,
        logs: Path,
        command: list[str],
        *,
        cwd: Path,
        environment: Mapping[str, str],
        thread_cwd: str,
        model: str,
        effort: str,
        persist: bool = False,
        config: dict[str, object] | None = None,
        instructions: str = "",
        resume_thread_id: str | None = None,
        resume_base_instructions: str = "",
        resume_instructions: str = "",
        responder: FrameResponder | None = None,
        dynamic_tools: list[dict[str, object]] | None = None,
    ):
        self._logs = logs
        self._command = command
        self._cwd = cwd
        self._environment = environment
        self._thread_cwd = thread_cwd
        self._model = model
        self._effort = effort
        self._persist = persist
        self._config = config
        self._instructions = instructions
        self._resume_thread_id = resume_thread_id
        self._resume_base_instructions = resume_base_instructions
        self._resume_instructions = resume_instructions
        self._responder = responder
        self._dynamic_tools = dynamic_tools
        self._process: AsyncNativeProcess | None = None
        self._harness: facade.CodexHarness | None = None
        self._thread_id_value: str | None = None

    async def __aenter__(self) -> CodexRun:
        process = AsyncNativeProcess(
            self._logs,
            self._command,
            cwd=self._cwd,
            environment=dict(self._environment),
            frame_responder=self._responder,
        )
        self._process = await process.__aenter__()
        self._harness = facade.CodexHarness(process, request_prefix="capture")
        try:
            await self._initialize()
            if self._resume_thread_id is None:
                receipt = await self._codex().start_thread(
                    cwd=self._thread_cwd,
                    model=self._model,
                    effort=self._effort,
                    persist=self._persist,
                    config=self._config,
                    instructions=self._instructions,
                    dynamic_tools=self._dynamic_tools,
                )
            else:
                receipt = await self._codex().resume_thread(
                    thread_id=self._resume_thread_id,
                    base_instructions=self._resume_base_instructions,
                    instructions=self._resume_instructions,
                )
            self._thread_id_value = _response_thread_id(_require(receipt))
        except BaseException:
            self._process, self._harness = None, None
            await process.close()
            raise
        return self

    async def __aexit__(self, *args: object) -> None:
        await self._native().__aexit__(*args)

    def _native(self) -> AsyncNativeProcess:
        if self._process is None:
            raise RuntimeError("Codex run was not started")
        return self._process

    def _codex(self) -> facade.CodexHarness:
        if self._harness is None:
            raise RuntimeError("Codex run was not started")
        return self._harness

    @property
    def running(self) -> bool:
        return self._native().alive()

    @property
    def thread_id(self) -> str:
        if self._thread_id_value is None:
            raise RuntimeError("Codex run did not start a thread")
        return self._thread_id_value

    def events(self) -> CodexEvents:
        return CodexEvents(self._native().frames())

    def native_frames(self) -> list[dict[str, Any]]:
        return self._native().stdout_frames()

    async def start_turn(self, text: str, *, model: str | None = None) -> CodexTurn:
        thread_id = self.thread_id
        events = self.events()
        response = _require(await self._codex().start_turn(thread_id=thread_id, text=text, model=model))
        result = wire.TurnResult.model_validate(response.result)
        return CodexTurn(thread_id, result.turn.id, events)

    async def steer(self, turn: CodexTurn, text: str) -> wire.Response:
        self._assert_turn(turn)
        return _require(await self._codex().steer(thread_id=turn.thread_id, turn_id=turn.id, text=text))

    async def interrupt(self, turn: CodexTurn) -> wire.Response:
        self._assert_turn(turn)
        return _require(await self._codex().interrupt(thread_id=turn.thread_id, turn_id=turn.id))

    async def crash(self) -> int:
        return await self._native().crash()

    async def _initialize(self) -> None:
        _require(await self._codex().initialize(experimental_api=self._dynamic_tools is not None))

    def _assert_turn(self, turn: CodexTurn) -> None:
        if turn.thread_id != self.thread_id:
            raise ValueError(f"turn {turn.id} does not belong to this Codex run")


def _response_thread_id(response: wire.Response) -> str:
    result = wire.ThreadResult.model_validate(response.result)
    return result.thread.id


def _require(receipt: facade.CodexReceipt) -> wire.Response:
    response = receipt.response
    if response.error is not None:
        raise RuntimeError(f"Codex request failed: {response.error}")
    return response
