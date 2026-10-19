"""A generated Runner gRPC peer for tests that exercise Python clients and App consumers.

This peer forwards calls through the already connected native Runner control tunnel. It
deliberately does not implement Gateway authentication, Sandbox lookup, or identity fencing.
"""

import asyncio
from collections import defaultdict
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import override

import grpc
from google.protobuf.message import Message

from agentplane.grpc.serving import serving
from agentplane.proto import runner_control_pb2, runner_control_pb2_grpc, runner_pb2
from agentplane.runner.testing.process import RunningRunner
from agentplane.runner_gateway.client import SandboxControlClient

TEST_TOKEN = "test-runner-peer-token"


class RunnerPeer(runner_control_pb2_grpc.RunnerServicer):
    """The generated Runner service surface, optionally backed by a native process."""

    def __init__(self, *, record_metadata: bool) -> None:
        self.runner: RunningRunner | None = None
        self.record_metadata = record_metadata
        self.metadata_calls: list[tuple[str, dict[str, tuple[str, ...]]]] = []
        self._list_sessions_rejections: list[tuple[grpc.StatusCode, str]] = []

    @contextmanager
    def using_runner(self, runner: RunningRunner) -> Iterator[None]:
        """Temporarily delegate RPCs through the supplied runner's control connection."""
        previous, self.runner = self.runner, runner
        try:
            yield
        finally:
            self.runner = previous

    def reject_next_list_sessions(self, code: grpc.StatusCode, details: str) -> None:
        """Script a ListSessions response for App error-propagation tests."""
        self._list_sessions_rejections.append((code, details))

    def _record(self, method: str, context: grpc.aio.ServicerContext) -> None:
        if not self.record_metadata:
            return
        values: defaultdict[str, list[str]] = defaultdict(list)
        for key, value in context.invocation_metadata() or ():
            text_value = value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value
            values[key].append(text_value)
        self.metadata_calls.append((method, {key: tuple(items) for key, items in values.items()}))

    async def _forward[Request: Message, Response: Message](
        self,
        method: str,
        request: Request,
        context: grpc.aio.ServicerContext,
        response_type: type[Response],
        invoke: Callable[[runner_control_pb2_grpc.RunnerAsyncStub, Request], Awaitable[Response]],
    ) -> Response:
        self._record(method, context)
        if method == "ListSessions" and self._list_sessions_rejections:
            code, details = self._list_sessions_rejections.pop(0)
            await context.abort(code, details)
        runner = self.runner
        if runner is None:
            if method == "ListSessions":
                return response_type()
            await context.abort(grpc.StatusCode.NOT_FOUND, "test peer has no Runner")
            raise AssertionError("gRPC abort returned")
        try:
            return await invoke(await runner.control(), request)
        except grpc.aio.AioRpcError as error:
            await context.abort(error.code(), error.details() or "")
            raise AssertionError("gRPC abort returned")

    @override
    async def ListSessions(
        self, request: runner_pb2.ListSessionsRequest, context: grpc.aio.ServicerContext
    ) -> runner_pb2.ListSessionsResponse:
        return await self._forward(
            "ListSessions", request, context, runner_pb2.ListSessionsResponse, lambda stub, req: stub.ListSessions(req)
        )

    @override
    async def GetStatus(
        self, request: runner_pb2.GetStatusRequest, context: grpc.aio.ServicerContext
    ) -> runner_pb2.RunnerStatus:
        return await self._forward(
            "GetStatus", request, context, runner_pb2.RunnerStatus, lambda stub, req: stub.GetStatus(req)
        )

    @override
    async def OpenSession(
        self, request: runner_control_pb2.OpenSessionRequest, context: grpc.aio.ServicerContext
    ) -> runner_control_pb2.OpenSessionResponse:
        return await self._forward(
            "OpenSession",
            request,
            context,
            runner_control_pb2.OpenSessionResponse,
            lambda stub, req: stub.OpenSession(req),
        )

    @override
    async def Quiesce(
        self, request: runner_pb2.QuiesceRequest, context: grpc.aio.ServicerContext
    ) -> runner_pb2.QuiesceResponse:
        return await self._forward(
            "Quiesce", request, context, runner_pb2.QuiesceResponse, lambda stub, req: stub.Quiesce(req)
        )

    @override
    async def SubmitInput(
        self, request: runner_control_pb2.SubmitInputRequest, context: grpc.aio.ServicerContext
    ) -> runner_control_pb2.SubmitInputResponse:
        return await self._forward(
            "SubmitInput",
            request,
            context,
            runner_control_pb2.SubmitInputResponse,
            lambda stub, req: stub.SubmitInput(req),
        )

    @override
    async def ChangeModel(
        self, request: runner_control_pb2.ChangeModelRequest, context: grpc.aio.ServicerContext
    ) -> runner_control_pb2.ChangeModelResponse:
        return await self._forward(
            "ChangeModel",
            request,
            context,
            runner_control_pb2.ChangeModelResponse,
            lambda stub, req: stub.ChangeModel(req),
        )

    @override
    async def ChangeReasoningEffort(
        self, request: runner_control_pb2.ChangeReasoningEffortRequest, context: grpc.aio.ServicerContext
    ) -> runner_control_pb2.ChangeReasoningEffortResponse:
        return await self._forward(
            "ChangeReasoningEffort",
            request,
            context,
            runner_control_pb2.ChangeReasoningEffortResponse,
            lambda stub, req: stub.ChangeReasoningEffort(req),
        )

    @override
    async def InterruptTurn(
        self, request: runner_control_pb2.InterruptTurnRequest, context: grpc.aio.ServicerContext
    ) -> runner_control_pb2.InterruptTurnResponse:
        return await self._forward(
            "InterruptTurn",
            request,
            context,
            runner_control_pb2.InterruptTurnResponse,
            lambda stub, req: stub.InterruptTurn(req),
        )


@dataclass(frozen=True)
class Endpoint:
    target: str
    token_file: Path
    peer: RunnerPeer

    def client(self) -> SandboxControlClient:
        return SandboxControlClient(
            control_target=self.target, token_file=self.token_file, request_timeout_s=20, lifecycle_timeout_s=310
        )

    @contextmanager
    def using_runner(self, runner: RunningRunner) -> Iterator[None]:
        with self.peer.using_runner(runner):
            yield


@asynccontextmanager
async def runner_peer(
    token_file: Path, *, runner: RunningRunner | None = None, record_metadata: bool = False, token: str = TEST_TOKEN
) -> AsyncIterator[Endpoint]:
    """Serve the generated Runner service on an ephemeral loopback port."""
    await asyncio.to_thread(token_file.write_text, token)
    peer = RunnerPeer(record_metadata=record_metadata)
    peer.runner = runner
    server = grpc.aio.server()
    runner_control_pb2_grpc.add_RunnerServicer_to_server(peer, server)
    async with serving(server, "127.0.0.1:0", grace_s=0) as port:
        yield Endpoint(f"127.0.0.1:{port}", token_file, peer)
