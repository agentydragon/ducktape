"""A typed client over one Attach stream: the runner API as its callers see it."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import grpc

from x.agentplane.runner import protocol_pb2 as pb, protocol_pb2_grpc

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf
# gazelle:include_dep @pypi//grpcio


class RunnerError(Exception):
    """The runner ended the stream with an error."""


class StreamClosedError(Exception):
    """The runner ended the stream without an error, after Shutdown or Detach."""


class Attachment:
    def __init__(
        self, call: grpc.aio.StreamStreamCall[pb.ClientMessage, pb.ServerMessage], attached: pb.Attached
    ) -> None:
        self._call = call
        self.attached = attached
        self.seen: list[pb.Event] = []

    @property
    def cursor(self) -> int:
        """The last sequence read; what a reconnecting Open passes as after_sequence."""
        return self.seen[-1].sequence if self.seen else 0

    async def send(self, input_id: str, text: str) -> None:
        await self._call.write(pb.ClientMessage(input=pb.Input(input_id=input_id, text=text)))

    async def interrupt(self) -> None:
        await self._call.write(pb.ClientMessage(interrupt=pb.Interrupt()))

    async def shutdown(self) -> None:
        await self._call.write(pb.ClientMessage(shutdown=pb.Shutdown()))

    async def detach(self) -> None:
        await self._call.write(pb.ClientMessage(detach=pb.Detach()))

    async def next_event(self) -> pb.Event:
        message = await self._call.read()
        if message is grpc.aio.EOF:
            raise StreamClosedError
        assert isinstance(message, pb.ServerMessage)
        if message.HasField("error"):
            raise RunnerError(message.error)
        assert message.HasField("event"), "an Attached message after the first is a protocol violation"
        self.seen.append(message.event)
        return message.event

    async def until(self, accept: Callable[[pb.Event], bool], *, timeout_s: float = 60) -> pb.Event:
        """Read events until one satisfies `accept`, and return it; earlier ones land in `seen`."""

        async def read() -> pb.Event:
            while not accept(event := await self.next_event()):
                pass
            return event

        return await asyncio.wait_for(read(), timeout=timeout_s)

    async def drain_until_end(self) -> None:
        """Read the remaining events of a stream the runner is ending."""
        try:
            while True:
                await self.next_event()
        except StreamClosedError:
            return

    def cancel(self) -> None:
        """Drop the connection without Detach, as a lost network path would."""
        self._call.cancel()


class RunnerClient:
    def __init__(self, target: str) -> None:
        self._channel = grpc.aio.insecure_channel(target)
        self._stub = protocol_pb2_grpc.RunnerStub(self._channel)

    async def attach(
        self, session_id: str, *, spec: pb.SessionSpec | None = None, after_sequence: int = 0
    ) -> Attachment:
        call = self._stub.Attach()
        await call.write(
            pb.ClientMessage(open=pb.Open(session_id=session_id, spec=spec, after_sequence=after_sequence))
        )
        message = await call.read()
        if message is grpc.aio.EOF:
            raise RunnerError("the runner ended the stream before answering Open")
        assert isinstance(message, pb.ServerMessage)
        if message.HasField("error"):
            raise RunnerError(message.error)
        assert message.HasField("attached"), "the first server message must be Attached"
        return Attachment(call, message.attached)

    async def initialize(self, key: str, script: str) -> pb.InitializeResult:
        return await self._stub.Initialize(pb.InitializeRequest(key=key, script=script))

    async def list_sessions(self) -> list[pb.SessionSummary]:
        response = await self._stub.ListSessions(pb.ListSessionsRequest())
        return list(response.sessions)

    async def close(self) -> None:
        await self._channel.close()
