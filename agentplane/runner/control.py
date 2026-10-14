"""The runner's side of the runner control stream: it serves the `Runner` service, dials the Runner
Gateway and keeps one `Connect` stream, over which the gateway calls that service. Contract:
`agentplane/docs/runner_control.md`."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import override
from uuid import uuid4

import grpc
from sqlalchemy.exc import SQLAlchemyError

from agentplane.grpc_tunnel import local_server, relay
from agentplane.proto import (
    command_pb2,
    event_log_pb2,
    grpc_tunnel_pb2,
    runner_control_pb2,
    runner_control_pb2_grpc,
    runner_pb2,
)
from agentplane.runner.journal import CommandConflictError, JournalSealedError
from agentplane.runner.reconnect import StreamLostError, StreamTiming, reconnect
from agentplane.runner.runner import (
    InitializationConflictError,
    InvalidRequestError,
    OpenError,
    Runner,
    SandboxSealedError,
    SessionUnknownError,
    SetupFailedError,
    SpecConflictError,
    build_attached,
    finish_command,
)
from agentplane.runner.session import Session

# gazelle:include_dep @pypi//protobuf

logger = logging.getLogger(__name__)


# Only an operator resolves a refused token, so it backs off at the maximum.
OPERATOR_CODES = frozenset({grpc.StatusCode.PERMISSION_DENIED})


class HarnessStoppedError(Exception):
    """Delivery never starts a stopped harness."""


# The first matching row answers; anything else fails the call with `UNKNOWN`, such as a harness that
# did not launch.
_REFUSALS: tuple[tuple[type[Exception] | tuple[type[Exception], ...], grpc.StatusCode], ...] = (
    (SessionUnknownError, grpc.StatusCode.NOT_FOUND),
    ((SpecConflictError, InitializationConflictError, CommandConflictError), grpc.StatusCode.ALREADY_EXISTS),
    (InvalidRequestError, grpc.StatusCode.INVALID_ARGUMENT),
    (
        (SandboxSealedError, JournalSealedError, SetupFailedError, HarnessStoppedError, OpenError),
        grpc.StatusCode.FAILED_PRECONDITION,
    ),
    # The state volume failed; a retry may find it working.
    ((OSError, SQLAlchemyError), grpc.StatusCode.UNAVAILABLE),
)


@asynccontextmanager
async def _refusals(context: grpc.aio.ServicerContext) -> AsyncIterator[None]:
    try:
        yield
    except Exception as error:
        for errors, code in _REFUSALS:
            if isinstance(error, errors):
                if code == grpc.StatusCode.UNAVAILABLE:
                    logger.exception("Runner call failed on the state volume")
                await context.abort(code, str(error))
        raise


def _attached(session: Session) -> runner_pb2.Attached:
    return build_attached(session, session.journal.recovery_state)


async def _admit(runner: Runner, request: runner_control_pb2.DeliverCommandRequest) -> event_log_pb2.EventEntry:
    """Admit a delivered command and return the journal's entry for it. A command already journaled
    with the same payload is answered with its existing entry, so redelivery is safe."""
    command = request.command
    if not command.command_id or command.WhichOneof("operation") is None:
        raise InvalidRequestError("command_id and operation required")
    session = await runner.stored_session(request.session_id)
    if session is None:
        raise SessionUnknownError("no such session")
    stored = await session.journal.get(command.command_id)
    if stored is None:
        if not session.running:
            raise HarnessStoppedError("the harness is not running")
        await finish_command(session.command(command))
    elif command_pb2.Command.FromString(stored.payload) != command:
        raise CommandConflictError("the command id was admitted for other work")
    return await session.journal.admitted_entry(command.command_id)


class RunnerService(runner_control_pb2_grpc.RunnerServicer):
    """The `Runner` service. A call whose caller is gone abandons its answer, not a half-done
    operation, which a repeated call only confirms."""

    def __init__(self, runner: Runner) -> None:
        self.runner = runner
        self._admissions: set[asyncio.Task[event_log_pb2.EventEntry]] = set()
        self._sealing: asyncio.Task[runner_pb2.SealResponse] | None = None

    async def _after_seal(self) -> None:
        # Ordering only: the Seal's outcome is its own call's to report.
        if self._sealing is not None:
            await asyncio.wait({self._sealing})

    @override
    async def ListSessions(
        self, request: runner_pb2.ListSessionsRequest, context: grpc.aio.ServicerContext
    ) -> runner_pb2.ListSessionsResponse:
        await self._after_seal()
        return runner_pb2.ListSessionsResponse(sessions=self.runner.summaries())

    @override
    async def OpenSession(
        self, request: runner_control_pb2.OpenSessionRequest, context: grpc.aio.ServicerContext
    ) -> runner_control_pb2.OpenSessionResponse:
        async with _refusals(context):
            await self._after_seal()
            if not request.HasField("spec"):
                raise InvalidRequestError("spec is required")
            setup_script = request.setup_script if request.HasField("setup_script") else None
            session = await finish_command(self.runner.open_session(request.session_id, request.spec, setup_script))
            return runner_control_pb2.OpenSessionResponse(attached=_attached(session))

    @override
    async def ResumeSession(
        self, request: runner_control_pb2.ResumeSessionRequest, context: grpc.aio.ServicerContext
    ) -> runner_control_pb2.ResumeSessionResponse:
        async with _refusals(context):
            await self._after_seal()
            session = await finish_command(self.runner.resume(request.session_id))
            return runner_control_pb2.ResumeSessionResponse(attached=_attached(session))

    async def _seal(self, context: grpc.aio.ServicerContext) -> runner_pb2.SealResponse:
        async def seal(preceding: set[asyncio.Task[event_log_pb2.EventEntry]]) -> runner_pb2.SealResponse:
            # Ordering only: an admission's outcome is its own call's to report.
            if preceding:
                await asyncio.wait(preceding)
            return await self.runner.seal()

        async with _refusals(context):
            self._sealing = sealing = asyncio.ensure_future(seal(set(self._admissions)))
            return await finish_command(sealing)

    @override
    async def Seal(self, request: runner_pb2.SealRequest, context: grpc.aio.ServicerContext) -> runner_pb2.SealResponse:
        return await self._seal(context)

    @override
    async def Quiesce(
        self, request: runner_pb2.QuiesceRequest, context: grpc.aio.ServicerContext
    ) -> runner_pb2.QuiesceResponse:
        return runner_pb2.QuiesceResponse(sessions=(await self._seal(context)).sessions)

    @override
    async def DeliverCommand(
        self, request: runner_control_pb2.DeliverCommandRequest, context: grpc.aio.ServicerContext
    ) -> runner_control_pb2.DeliverCommandResponse:
        async with _refusals(context):
            await self._after_seal()
            admission = asyncio.ensure_future(_admit(self.runner, request))
            self._admissions.add(admission)
            admission.add_done_callback(self._admissions.discard)
            return runner_control_pb2.DeliverCommandResponse(admission=await finish_command(admission))


class RunnerControlClient:
    """Serves `runner` on one `Connect` stream to the Runner Gateway for as long as the runner runs.

    Each connection dials a fresh HTTP/2 connection from a local subchannel pool: gRPC's global pool
    would hand a reconnect the stale connection that just failed.
    """

    def __init__(
        self,
        runner: runner_control_pb2_grpc.RunnerServicer,
        *,
        target: str,
        bearer: str,
        timing: StreamTiming,
        sandbox: Runner,
    ) -> None:
        self.runner = runner
        self.sandbox = sandbox
        self.target = target
        # A placeholder the egress proxy swaps for this Pod's control token; the runner never holds it.
        self.bearer = bearer
        self.timing = timing
        self.boot_id = str(uuid4())

    def facts(self) -> runner_control_pb2.RunnerFacts:
        return runner_control_pb2.RunnerFacts(bootstrap=self.sandbox.bootstrap_fact())

    async def run(self) -> None:
        async with local_server(
            lambda server: runner_control_pb2_grpc.add_RunnerServicer_to_server(self.runner, server)
        ) as runner:

            async def attempt() -> bool:
                # A connection that carried traffic returns and resets the backoff.
                await self.connect(runner)
                return False

            await reconnect(
                attempt, timing=self.timing, operator_codes=OPERATOR_CODES, describe="runner control stream"
            )

    async def connect(self, runner: grpc.aio.Channel) -> None:
        loop = asyncio.get_running_loop()
        welcomed_at: float | None = None
        lost = StreamLostError(None, "the stream ended")
        async with grpc.aio.insecure_channel(self.target, options=(("grpc.use_local_subchannel_pool", 1),)) as channel:
            try:
                stub = runner_control_pb2_grpc.RunnerControlStub(channel)
                call = stub.Connect(metadata=(("authorization", f"Bearer {self.bearer}"),))
                await call.write(
                    runner_control_pb2.RunnerFrame(hello=runner_control_pb2.Hello(runner_boot_id=self.boot_id))
                )
                async with asyncio.timeout(self.timing.liveness_s):
                    first = await call.read()
                if not isinstance(first, runner_control_pb2.ServiceFrame):
                    raise StreamLostError(await call.code(), await call.details())
                if first.WhichOneof("frame") != "welcome":
                    raise StreamLostError(None, f"expected Welcome, got {first.WhichOneof('frame')}")
                self.sandbox.adopt_incarnation(first.welcome.incarnation_uid or None)
                welcomed_at = loop.time()
                logger.info("runner control stream connected at epoch %d", first.welcome.connection_epoch)
                await self.serve(call, runner)
            except grpc.aio.AioRpcError as error:
                lost = StreamLostError(error.code(), error.details() or "")
            except TimeoutError:
                lost = StreamLostError(None, "no frame from the Runner Gateway")
            except StreamLostError as error:
                lost = error
        if welcomed_at is not None and loop.time() - welcomed_at >= self.timing.liveness_s:
            logger.warning("runner control stream ended: %s", lost)
            return
        raise lost

    async def serve(
        self,
        call: grpc.aio.StreamStreamCall[runner_control_pb2.RunnerFrame, runner_control_pb2.ServiceFrame],
        runner: grpc.aio.Channel,
    ) -> None:
        """Answer the gateway's calls and keep the stream alive until it ends; always raises. Calls the
        connection leaves unanswered go unanswered: the gateway calls again."""
        loop = asyncio.get_running_loop()
        last_frame = loop.time()
        writing = asyncio.Lock()

        async def send(frame: runner_control_pb2.RunnerFrame) -> None:
            async with writing:
                await call.write(frame)

        async def answer(tunneled: grpc_tunnel_pb2.Call) -> None:
            await send(runner_control_pb2.RunnerFrame(reply=await relay(tunneled, runner)))

        async def read(tasks: asyncio.TaskGroup) -> None:
            nonlocal last_frame
            while isinstance(frame := await call.read(), runner_control_pb2.ServiceFrame):
                last_frame = loop.time()
                match frame.WhichOneof("frame"):
                    case "heartbeat":
                        pass
                    case "call":
                        tasks.create_task(answer(frame.call))
                    case kind:
                        raise StreamLostError(grpc.StatusCode.INVALID_ARGUMENT, f"unexpected {kind} frame")
            raise StreamLostError(await call.code(), await call.details())

        async def publish_facts() -> None:
            # Each frame replaces the gateway's record of every fact, so each carries all of them.
            await send(runner_control_pb2.RunnerFrame(facts=self.facts()))
            await self.sandbox.wait_bootstrap_settled()
            await send(runner_control_pb2.RunnerFrame(facts=self.facts()))

        async def keep_alive() -> None:
            while True:
                await asyncio.sleep(self.timing.heartbeat_s)
                if loop.time() - last_frame > self.timing.liveness_s:
                    raise TimeoutError
                await send(runner_control_pb2.RunnerFrame(heartbeat=runner_control_pb2.Heartbeat()))

        try:
            async with asyncio.TaskGroup() as tasks:
                tasks.create_task(read(tasks))
                tasks.create_task(publish_facts())
                tasks.create_task(keep_alive())
        except* (grpc.aio.AioRpcError, StreamLostError, TimeoutError) as group:
            # The first failure ends the connection; the rest are its consequences.
            raise group.exceptions[0] from None
        finally:
            call.cancel()
