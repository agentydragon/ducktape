"""A test-only Claude Code run with typed, independent observations of its native trace."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from agentplane.native.async_process import AsyncNativeProcess, FrameCursor, FrameResponder
from agentplane.native.claude import blocks, facade, wire
from agentplane.native.claude.scenarios import HOOK_EVENTS


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
        sdk_mcp_servers: list[str] | None = None,
        initialize: bool = True,
    ):
        self._logs = logs
        self._command = command
        self._cwd = cwd
        self._environment = environment
        self._responder = responder
        self._hooks = hooks
        self._sdk_mcp_servers = sdk_mcp_servers
        self._initialize = initialize
        self._process: AsyncNativeProcess | None = None
        self._harness: facade.ClaudeHarness | None = None

    async def __aenter__(self) -> ClaudeRun:
        process = AsyncNativeProcess(
            self._logs,
            self._command,
            cwd=self._cwd,
            environment=dict(self._environment),
            frame_responder=self._responder,
        )
        self._process = await process.__aenter__()
        self._harness = facade.ClaudeHarness(process)
        try:
            if self._initialize:
                response = await self.initialize()
                if not isinstance(response, wire.ControlResponseFrame):
                    raise RuntimeError(f"Claude initialization failed: {response}")
        except BaseException:
            self._process, self._harness = None, None
            await process.close()
            raise
        return self

    async def __aexit__(self, *args: object) -> None:
        await self._native().__aexit__(*args)

    def _native(self) -> AsyncNativeProcess:
        if self._process is None:
            raise RuntimeError("Claude run was not started")
        return self._process

    def _claude(self) -> facade.ClaudeHarness:
        if self._harness is None:
            raise RuntimeError("Claude run was not started")
        return self._harness

    @property
    def running(self) -> bool:
        return self._native().alive()

    def events(self) -> ClaudeEvents:
        return ClaudeEvents(self._native().frames())

    def native_frames(self) -> list[dict[str, Any]]:
        return self._native().stdout_frames()

    async def initialize(self) -> wire.ControlResponseFrame | wire.ResultFrame:
        receipt = await self._claude().initialize(
            hooks={event: [f"capture-{event}"] for event in HOOK_EVENTS} if self._hooks else None,
            sdk_mcp_servers=self._sdk_mcp_servers,
        )
        return receipt.response

    async def send(self, text: str) -> ClaudeInput:
        events = self.events()
        frame = await self._claude().submit(text)
        return ClaudeInput(frame, events)

    async def send_many(self, texts: list[str]) -> list[ClaudeInput]:
        events = [self.events() for _ in texts]
        frames = await self._claude().submit_many(texts)
        return [ClaudeInput(frame, observer) for frame, observer in zip(frames, events, strict=True)]

    async def interrupt(self, *, cancel_queued: bool) -> wire.ControlResponseFrame:
        response = (await self._claude().interrupt(cancel_queued=cancel_queued)).response
        if not isinstance(response, wire.ControlResponseFrame):
            raise RuntimeError(f"Claude interrupt failed: {response}")
        return response

    async def set_model(self, model: str) -> wire.ControlResponseFrame:
        response = (await self._claude().set_model(model)).response
        if not isinstance(response, wire.ControlResponseFrame):
            raise RuntimeError(f"Claude model switch failed: {response}")
        return response

    async def crash(self) -> int:
        return await self._native().crash()
