"""A test-only Claude Code run with typed, independent observations of its native trace."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from agentplane.native.claude import blocks, driver, wire
from agentplane.native.claude.scenarios import HOOK_EVENTS
from agentplane.native.testing.async_process import AsyncNativeProcess, FrameCursor, FrameResponder


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

    async def __aenter__(self) -> ClaudeRun:
        process = AsyncNativeProcess(
            self._logs,
            self._command,
            cwd=self._cwd,
            environment=dict(self._environment),
            frame_responder=self._responder,
        )
        self._process = await process.__aenter__()
        try:
            if self._initialize:
                await self.initialize()
        except BaseException:
            self._process = None
            await process.close()
            raise
        return self

    async def __aexit__(self, exc_type: type[BaseException] | None, *args: object) -> None:
        await self._native().__aexit__(exc_type, *args)

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

    def frames(self) -> FrameCursor:
        return self._native().frames()

    async def initialize(self) -> wire.ControlResponseFrame:
        request = driver.initialize(
            hooks={event: [f"capture-{event}"] for event in HOOK_EVENTS} if self._hooks else None,
            sdk_mcp_servers=self._sdk_mcp_servers,
        )
        # Resuming can emit task-notification results before the initialization reply. Only this
        # request's control response completes the handshake; EOF or the request timeout handles a
        # process that never sends it.
        response = await self._request(request)
        if response.response.subtype == "error":
            raise RuntimeError(f"Claude initialization rejected: {response.response.error}")
        return response

    async def send(self, text: str) -> ClaudeInput:
        return await self.send_frame(driver.user_frame(text))

    async def send_frame(self, frame: wire.UserInput) -> ClaudeInput:
        """Send a constructed user frame verbatim for bounded protocol probes."""
        events = self.events()
        await self._native().send(frame)
        return ClaudeInput(frame, events)

    async def send_many(self, texts: list[str]) -> list[ClaudeInput]:
        events = [self.events() for _ in texts]
        frames = [driver.user_frame(text) for text in texts]
        for frame in frames:
            await self._native().send(frame)
        return [ClaudeInput(frame, observer) for frame, observer in zip(frames, events, strict=True)]

    async def interrupt(self, *, cancel_queued: bool) -> wire.ControlResponseFrame:
        return await self._request(driver.interrupt(cancel_queued=cancel_queued))

    async def set_model(self, model: str) -> wire.ControlResponseFrame:
        return await self._request(driver.set_model(model))

    async def set_effort(self, effort: str) -> wire.ControlResponseFrame:
        return await self._request(driver.set_effort(effort))

    async def crash(self) -> int:
        return await self._native().crash()

    async def _request(
        self,
        request: wire.InitializeRequest | wire.InterruptRequest | wire.SetModelRequest | wire.ApplyFlagSettingsRequest,
    ) -> wire.ControlResponseFrame:
        """Send one control request and return the control response that names its `request_id`."""
        receipt = await self._native().request(
            request, matches=lambda frame: _is_control_response(frame, request.request_id)
        )
        return wire.ControlResponseFrame.model_validate(receipt.frame)


def _is_control_response(frame: dict[str, Any], request_id: str) -> bool:
    response = frame.get("response")
    return (
        frame.get("type") == "control_response"
        and isinstance(response, dict)
        and response.get("request_id") == request_id
    )
