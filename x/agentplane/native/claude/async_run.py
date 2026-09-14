"""A test-only Claude Code run with typed, independent observations of its native trace."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from x.agentplane.native.async_process import AsyncNativeProcess, FrameCursor, FrameResponder
from x.agentplane.native.claude import blocks, driver, wire
from x.agentplane.native.claude.scenarios import HOOK_EVENTS


class ClaudeEvents:
    """One cursor over a Claude run's append-only native event trace."""

    def __init__(self, frames: FrameCursor):
        self._frames = frames

    async def next(self) -> wire.ClaudeFrame:
        return wire.parse_frame(await self._frames.next())

    async def control_response(self, request_id: str) -> wire.ControlResponseFrame:
        while True:
            match await self.next():
                case wire.ControlResponseFrame(response=wire.ControlResponseBody(request_id=received)) as frame:
                    if received == request_id:
                        return frame

    async def lifecycle(self, command_uuid: str, state: wire.CommandState) -> wire.CommandLifecycleFrame:
        while True:
            match await self.next():
                case wire.CommandLifecycleFrame(command_uuid=received, state=received_state) as frame:
                    if received == command_uuid and received_state is state:
                        return frame

    async def result(self) -> wire.ResultFrame:
        while True:
            match await self.next():
                case wire.ResultFrame() as frame:
                    return frame

    async def active(self) -> wire.StreamEventFrame | wire.AssistantFrame:
        while True:
            match await self.next():
                case wire.StreamEventFrame() as frame:
                    return frame
                case wire.AssistantFrame(message=wire.AssistantMessage(content=content)) as frame:
                    if any(isinstance(block, blocks.ToolUseBlock) for block in content):
                        return frame


class ClaudeInput:
    """One submitted user input and the events emitted after its submission."""

    def __init__(self, frame: wire.UserInput, events: ClaudeEvents):
        self.frame = frame
        self._events = events

    @property
    def uuid(self) -> str:
        return self.frame.uuid

    async def lifecycle(self, state: wire.CommandState) -> wire.CommandLifecycleFrame:
        return await self._events.lifecycle(self.uuid, state)

    async def result(self) -> wire.ResultFrame:
        return await self._events.result()

    async def active(self) -> wire.StreamEventFrame | wire.AssistantFrame:
        return await self._events.active()


class ClaudeRun:
    """One native Claude harness process, including its stream-json initialization handshake."""

    def __init__(
        self,
        logs: Path,
        command: list[str],
        *,
        cwd: Path,
        environment: Mapping[str, str],
        responder: FrameResponder | None = None,
        hooks: bool = False,
        initialize: bool = True,
    ):
        self._logs = logs
        self._command = command
        self._cwd = cwd
        self._environment = environment
        self._responder = responder
        self._hooks = hooks
        self._initialize = initialize
        self._process: AsyncNativeProcess | None = None

    async def __aenter__(self) -> ClaudeRun:
        process = AsyncNativeProcess(self._logs, self._command, cwd=self._cwd, environment=dict(self._environment))
        process.frame_responder = self._responder
        self._process = await process.__aenter__()
        if self._initialize:
            response = await self.initialize()
            if not isinstance(response, wire.ControlResponseFrame):
                raise RuntimeError(f"Claude initialization failed: {response}")
        return self

    async def __aexit__(self, *args: object) -> None:
        await self._native().__aexit__(*args)

    def _native(self) -> AsyncNativeProcess:
        if self._process is None:
            raise RuntimeError("Claude run was not started")
        return self._process

    @property
    def running(self) -> bool:
        return self._native().alive()

    def events(self) -> ClaudeEvents:
        return ClaudeEvents(self._native().frames())

    def native_frames(self) -> list[dict[str, Any]]:
        return self._native().stdout_frames()

    async def initialize(self) -> wire.ControlResponseFrame | wire.ResultFrame:
        events = self.events()
        request = driver.initialize(
            hooks={event: [f"capture-{event}"] for event in HOOK_EVENTS} if self._hooks else None
        )
        await self._native().send(request)
        while True:
            match await events.next():
                case wire.ControlResponseFrame(response=wire.ControlResponseBody(request_id=request_id)) as frame:
                    if request_id == request.request_id:
                        return frame
                case wire.ResultFrame() as frame:
                    return frame

    async def send(self, text: str) -> ClaudeInput:
        events = self.events()
        frame = driver.user_frame(text)
        await self._native().send(frame)
        return ClaudeInput(frame, events)

    async def send_many(self, texts: list[str]) -> list[ClaudeInput]:
        inputs = [ClaudeInput(driver.user_frame(text), self.events()) for text in texts]
        await self._native().send_many([item.frame for item in inputs])
        return inputs

    async def interrupt(self, *, cancel_queued: bool) -> wire.ControlResponseFrame:
        events = self.events()
        request = driver.interrupt(cancel_queued=cancel_queued)
        await self._native().send(request)
        return await events.control_response(request.request_id)

    async def set_model(self, model: str) -> wire.ControlResponseFrame:
        events = self.events()
        request = driver.set_model(model)
        await self._native().send(request)
        return await events.control_response(request.request_id)

    async def crash(self) -> int:
        return await self._native().crash()
