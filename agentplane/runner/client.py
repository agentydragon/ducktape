"""Typed runner transport client over one Attach stream."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from types import TracebackType
from typing import Self

import grpc

from agentplane.protocol import command_pb2, event_log_pb2
from agentplane.runner import protocol_pb2, protocol_pb2_grpc

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf
# gazelle:include_dep @pypi//grpcio

# How long the runner may take to answer Open with Attached. An Open without a spec is answered from
# the session's journal head, with no native work in between.
OBSERVE_ANSWER_S = 10
# One with a spec may first launch the harness. The runner gives each native handshake request 60 s
# (`Session.request`) and Codex's handshake makes two, so a live runner reports its own failure first.
LAUNCH_ANSWER_S = 150


class RunnerError(Exception):
    """The runner ended the stream with an error."""


class OpenTimeoutError(RunnerError):
    """The runner accepted Attach but did not answer Open in time: wedged, or a half-open connection."""


class StreamClosedError(Exception):
    """The runner ended the stream without an error, after StopRunnerSession or Detach."""


class Attachment:
    def __init__(
        self,
        call: grpc.aio.StreamStreamCall[protocol_pb2.ClientMessage, protocol_pb2.ServerMessage],
        attached: protocol_pb2.Attached,
        *,
        after_cursor: int = 0,
        capture_history: bool = False,
    ) -> None:
        self._call = call
        self.attached = attached
        self.seen: list[event_log_pb2.EventEntry] = []
        self._capture_history = capture_history
        self._cursor = after_cursor
        self._detach_sent = False

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        if exc_type is not None:
            # Cleanup must not replace the caller's original failure.
            self.cancel()
            return
        await self.detach()
        await self.drain_until_end()

    @property
    def cursor(self) -> int:
        """The last source-local cursor read; a reconnecting Open follows after it."""
        return self._cursor

    async def send(self, command_id: str, text: str) -> None:
        await self.command(command_pb2.Command(command_id=command_id, submit_input=command_pb2.SubmitInput(text=text)))

    async def command(self, command: command_pb2.Command) -> None:
        await self._call.write(protocol_pb2.ClientMessage(command=command))

    async def interrupt(self, command_id: str, turn_id: str) -> None:
        await self.command(
            command_pb2.Command(command_id=command_id, interrupt_turn=command_pb2.InterruptTurn(turn_id=turn_id))
        )

    async def switch_model(self, command_id: str, model: str) -> None:
        await self.command(
            command_pb2.Command(command_id=command_id, change_model=command_pb2.ChangeModel(model=model))
        )

    async def stop_runner_session(self, command_id: str) -> None:
        await self.command(
            command_pb2.Command(command_id=command_id, stop_runner_session=command_pb2.StopRunnerSession())
        )

    async def detach(self) -> None:
        """Ask the runner to end this attachment, at most once."""
        if self._detach_sent:
            return
        await self._call.write(protocol_pb2.ClientMessage(detach=protocol_pb2.Detach()))
        self._detach_sent = True

    async def next_entry(self) -> event_log_pb2.EventEntry:
        message = await self._call.read()
        if message is grpc.aio.EOF:
            raise StreamClosedError
        assert isinstance(message, protocol_pb2.ServerMessage)
        if message.HasField("error"):
            raise RunnerError(message.error)
        assert message.HasField("event_entry"), "an Attached message after the first is a protocol violation"
        self._cursor = message.event_entry.cursor
        if self._capture_history:
            self.seen.append(message.event_entry)
        return message.event_entry

    async def until(
        self, accept: Callable[[event_log_pb2.EventEntry], bool], *, timeout_s: float = 60
    ) -> event_log_pb2.EventEntry:
        """Read entries until one satisfies `accept`, and return it; history capture is opt-in."""

        async def read() -> event_log_pb2.EventEntry:
            while not accept(entry := await self.next_entry()):
                pass
            return entry

        return await asyncio.wait_for(read(), timeout=timeout_s)

    async def drain_until_end(self) -> None:
        """Read the remaining entries of a stream the runner is ending."""
        try:
            while True:
                await self.next_entry()
        except StreamClosedError:
            return

    def cancel(self) -> None:
        """Drop the connection without Detach, as a lost network path would."""
        self._call.cancel()


class RunnerClient:
    def __init__(self, target: str, *, capture_history: bool = False) -> None:
        self._capture_history = capture_history
        self._channel = grpc.aio.insecure_channel(target)
        self._stub = protocol_pb2_grpc.RunnerStub(self._channel)

    async def attach(
        self, session_id: str, *, spec: protocol_pb2.SessionSpec | None = None, after_cursor: int = 0
    ) -> Attachment:
        call = self._stub.Attach()
        # Only the handshake is bounded: the same stream then follows the session for as long as
        # its caller reads, so a deadline on the call itself would cut that off.
        answer_s = OBSERVE_ANSWER_S if spec is None else LAUNCH_ANSWER_S
        try:
            async with asyncio.timeout(answer_s):
                await call.write(
                    protocol_pb2.ClientMessage(
                        open=protocol_pb2.Open(
                            session_id=session_id, spec=spec, follow=event_log_pb2.Follow(after_cursor=after_cursor)
                        )
                    )
                )
                message = await call.read()
        except TimeoutError as error:
            call.cancel()
            raise OpenTimeoutError(
                f"the runner did not answer Open for session {session_id!r} within {answer_s} seconds"
            ) from error
        if message is grpc.aio.EOF:
            raise RunnerError("the runner ended the stream before answering Open")
        assert isinstance(message, protocol_pb2.ServerMessage)
        if message.HasField("error"):
            raise RunnerError(message.error)
        assert message.HasField("attached"), "the first server message must be Attached"
        return Attachment(call, message.attached, after_cursor=after_cursor, capture_history=self._capture_history)

    def initialize_events(
        self, script: str, *, after_sequence: int = 0
    ) -> grpc.aio.UnaryStreamCall[protocol_pb2.InitializeRequest, protocol_pb2.InitializationEvent]:
        """Replay initialization events after a cursor, then follow live output through completion."""
        return self._stub.Initialize(protocol_pb2.InitializeRequest(script=script, after_sequence=after_sequence))

    async def initialize(self, script: str) -> protocol_pb2.InitializeResult:
        """Run or replay initialization and return its terminal result."""
        result: protocol_pb2.InitializeResult | None = None
        async for event in self.initialize_events(script):
            if event.HasField("result"):
                result = event.result
        if result is None:
            raise RunnerError("the initialization stream ended without a result")
        return result

    async def list_sessions(self) -> list[protocol_pb2.SessionSummary]:
        response = await self._stub.ListSessions(protocol_pb2.ListSessionsRequest())
        return list(response.sessions)

    async def close(self) -> None:
        await self._channel.close()
