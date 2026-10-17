"""A generated Runner gRPC peer for tests that exercise Python clients and App consumers.

This peer delegates to the Runner's own ``RunnerService``. It deliberately does not
implement Gateway authentication, Sandbox lookup, or identity fencing.
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
from agentplane.runner.control import RunnerService
from agentplane.runner.runner import Runner
from agentplane.runner_gateway.client import SandboxControlClient

TEST_TOKEN = "test-runner-peer-token"


class RunnerPeer(runner_control_pb2_grpc.RunnerServicer):
    """The generated Runner service surface, optionally backed by a native Runner."""

    def __init__(self, *, record_metadata: bool) -> None:
        self.runner: RunnerService | None = None
        self.record_metadata = record_metadata
        self.metadata_calls: list[tuple[str, dict[str, tuple[str, ...]]]] = []
        self._list_sessions_rejections: list[tuple[grpc.StatusCode, str]] = []

    @contextmanager
    def using_runner(self, runner: Runner) -> Iterator[None]:
        """Temporarily delegate RPCs to the supplied real Runner."""
        with self.using_service(RunnerService(runner)):
            yield

    @contextmanager
    def using_service(self, service: RunnerService) -> Iterator[None]:
        previous, self.runner = self.runner, service
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
        invoke: Callable[[RunnerService, Request, grpc.aio.ServicerContext], Awaitable[Response]],
    ) -> Response:
        self._record(method, context)
        if method == "ListSessions" and self._list_sessions_rejections:
            code, details = self._list_sessions_rejections.pop(0)
            await context.abort(code, details)
        if self.runner is None:
            if method == "ListSessions":
                return response_type()
            await context.abort(grpc.StatusCode.NOT_FOUND, "test peer has no Runner")
        return await invoke(self.runner, request, context)

    @override
    async def ListSessions(
        self, request: runner_pb2.ListSessionsRequest, context: grpc.aio.ServicerContext
    ) -> runner_pb2.ListSessionsResponse:
        return await self._forward(
            "ListSessions", request, context, runner_pb2.ListSessionsResponse, RunnerService.ListSessions
        )

    @override
    async def OpenSession(
        self, request: runner_control_pb2.OpenSessionRequest, context: grpc.aio.ServicerContext
    ) -> runner_control_pb2.OpenSessionResponse:
        return await self._forward(
            "OpenSession", request, context, runner_control_pb2.OpenSessionResponse, RunnerService.OpenSession
        )

    @override
    async def ResumeSession(
        self, request: runner_control_pb2.ResumeSessionRequest, context: grpc.aio.ServicerContext
    ) -> runner_control_pb2.ResumeSessionResponse:
        return await self._forward(
            "ResumeSession", request, context, runner_control_pb2.ResumeSessionResponse, RunnerService.ResumeSession
        )

    @override
    async def Quiesce(
        self, request: runner_pb2.QuiesceRequest, context: grpc.aio.ServicerContext
    ) -> runner_pb2.QuiesceResponse:
        return await self._forward("Quiesce", request, context, runner_pb2.QuiesceResponse, RunnerService.Quiesce)

    @override
    async def SubmitInput(
        self, request: runner_control_pb2.SubmitInputRequest, context: grpc.aio.ServicerContext
    ) -> runner_control_pb2.SubmitInputResponse:
        return await self._forward(
            "SubmitInput", request, context, runner_control_pb2.SubmitInputResponse, RunnerService.SubmitInput
        )

    @override
    async def ChangeModel(
        self, request: runner_control_pb2.ChangeModelRequest, context: grpc.aio.ServicerContext
    ) -> runner_control_pb2.ChangeModelResponse:
        return await self._forward(
            "ChangeModel", request, context, runner_control_pb2.ChangeModelResponse, RunnerService.ChangeModel
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
            RunnerService.ChangeReasoningEffort,
        )

    @override
    async def InterruptTurn(
        self, request: runner_control_pb2.InterruptTurnRequest, context: grpc.aio.ServicerContext
    ) -> runner_control_pb2.InterruptTurnResponse:
        return await self._forward(
            "InterruptTurn", request, context, runner_control_pb2.InterruptTurnResponse, RunnerService.InterruptTurn
        )

    @override
    async def StopRunnerSession(
        self, request: runner_control_pb2.StopRunnerSessionRequest, context: grpc.aio.ServicerContext
    ) -> runner_control_pb2.StopRunnerSessionResponse:
        return await self._forward(
            "StopRunnerSession",
            request,
            context,
            runner_control_pb2.StopRunnerSessionResponse,
            RunnerService.StopRunnerSession,
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
    def using_runner(self, runner: Runner) -> Iterator[None]:
        with self.peer.using_runner(runner):
            yield


@asynccontextmanager
async def runner_peer(
    token_file: Path, *, runner: Runner | None = None, record_metadata: bool = False, token: str = TEST_TOKEN
) -> AsyncIterator[Endpoint]:
    """Serve the generated Runner service on an ephemeral loopback port."""
    await asyncio.to_thread(token_file.write_text, token)
    peer = RunnerPeer(record_metadata=record_metadata)
    if runner is not None:
        peer.runner = RunnerService(runner)
    server = grpc.aio.server()
    runner_control_pb2_grpc.add_RunnerServicer_to_server(peer, server)
    async with serving(server, "127.0.0.1:0", grace_s=0) as port:
        yield Endpoint(f"127.0.0.1:{port}", token_file, peer)
