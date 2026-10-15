"""The runner's side of the runner control stream: it serves the `Runner` service, dials the Runner
Gateway and keeps one `Connect` stream, over which the gateway calls that service. Contract:
`agentplane/docs/runner_control.md`."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import override
from uuid import uuid4

import grpc
from google.protobuf.timestamp_pb2 import Timestamp
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
    SandboxNotIdleError,
    SandboxQuiescedError,
    SessionUnknownError,
    SetupFailedError,
    SpecConflictError,
    build_attached,
    finish_command,
)
from agentplane.runner.session import DRAINING_OPERATIONS, Session

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
    # The Sandbox is tearing down: the same call succeeds once it resumes.
    (SandboxQuiescedError, grpc.StatusCode.ABORTED),
    # A conditional Quiesce found the Sandbox working: the caller resets its idle timer.
    (SandboxNotIdleError, grpc.StatusCode.ABORTED),
    ((JournalSealedError, SetupFailedError, HarnessStoppedError, OpenError), grpc.StatusCode.FAILED_PRECONDITION),
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


async def _admit(
    runner: Runner, session_id: str, command: command_pb2.Command, *, in_lameduck: bool
) -> event_log_pb2.EventEntry:
    """Admit a delivered command and return the journal's entry for it. A command already journaled
    with the same payload is answered with its existing entry, so redelivery is safe."""
    # A delivery under way when the Quiesce arrived is admitted; the Quiesce waits for it.
    if not command.command_id or command.WhichOneof("operation") is None:
        raise InvalidRequestError("command_id and operation required")
    session = await runner.stored_session(session_id)
    if session is None:
        raise SessionUnknownError("no such session")
    stored = await session.journal.get(command.command_id)
    if stored is None:
        if in_lameduck and command.WhichOneof("operation") not in DRAINING_OPERATIONS:
            runner.refuse_work()
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
        self._admissions: set[asyncio.Future[None]] = set()
        self._quiescing: asyncio.Task[None] | None = None

    @asynccontextmanager
    async def _admission(self) -> AsyncIterator[None]:
        admission = asyncio.get_running_loop().create_future()
        self._admissions.add(admission)
        try:
            with self.runner.admitting_work():
                yield
        finally:
            self._admissions.discard(admission)
            admission.set_result(None)

    def _quiesce_finished(self, task: asyncio.Task[None]) -> None:
        error = None if task.cancelled() else task.exception()
        if task is not self._quiescing:
            return
        if task.cancelled():
            logger.error("Runner quiesce drain was cancelled; a later Quiesce can retry")
            self._quiescing = None
        elif error is not None:
            logger.error("Runner quiesce drain failed; a later Quiesce can retry", exc_info=error)
            self._quiescing = None

    def _start_quiesce(self) -> None:
        preceding: set[asyncio.Future[None]] = set(self._admissions)

        async def quiesce() -> None:
            # Ordering only: an admission's outcome is its own call's to report.
            if preceding:
                await asyncio.wait(preceding)
            await self.runner.quiesce()

        task = asyncio.create_task(quiesce(), name="runner quiesce drain")
        self._quiescing = task
        task.add_done_callback(self._quiesce_finished)

    async def _after_quiesce(self) -> None:
        # Ordering only: the Quiesce's outcome is its own call's to report.
        if self._quiescing is not None:
            await asyncio.wait({self._quiescing})

    @override
    async def ListSessions(
        self, request: runner_pb2.ListSessionsRequest, context: grpc.aio.ServicerContext
    ) -> runner_pb2.ListSessionsResponse:
        await self._after_quiesce()
        return runner_pb2.ListSessionsResponse(sessions=self.runner.summaries())

    @override
    async def OpenSession(
        self, request: runner_control_pb2.OpenSessionRequest, context: grpc.aio.ServicerContext
    ) -> runner_control_pb2.OpenSessionResponse:
        async with _refusals(context):
            self.runner.refuse_work()
            if not request.HasField("spec"):
                raise InvalidRequestError("spec is required")
            setup_script = request.setup_script if request.HasField("setup_script") else None
            async with self._admission():
                session = await finish_command(self.runner.open_session(request.session_id, request.spec, setup_script))
            return runner_control_pb2.OpenSessionResponse(attached=_attached(session))

    @override
    async def ResumeSession(
        self, request: runner_control_pb2.ResumeSessionRequest, context: grpc.aio.ServicerContext
    ) -> runner_control_pb2.ResumeSessionResponse:
        async with _refusals(context):
            self.runner.refuse_work()
            async with self._admission():
                session = await finish_command(self.runner.resume(request.session_id))
            return runner_control_pb2.ResumeSessionResponse(attached=_attached(session))

    async def _quiesce(
        self, context: grpc.aio.ServicerContext, if_idle_since: Timestamp | None
    ) -> runner_pb2.QuiesceResponse:
        async with _refusals(context):
            if if_idle_since is None:
                self.runner.enter_lameduck()
            else:
                self.runner.enter_lameduck_if_idle_since(if_idle_since)
            quiescing = self._quiescing
            if quiescing is None or (quiescing.done() and (quiescing.cancelled() or quiescing.exception() is not None)):
                self._start_quiesce()
            return runner_pb2.QuiesceResponse(stage=int(self.runner.facts().quiesce_stage))

    async def _deliver(
        self, session_id: str, command: command_pb2.Command, context: grpc.aio.ServicerContext
    ) -> event_log_pb2.EventEntry:
        async with _refusals(context):
            with self.runner.delivering():
                in_lameduck = self.runner.lameduck
                async with self._admission():
                    admission = asyncio.ensure_future(_admit(self.runner, session_id, command, in_lameduck=in_lameduck))
                    return await finish_command(admission)

    @override
    async def Quiesce(
        self, request: runner_pb2.QuiesceRequest, context: grpc.aio.ServicerContext
    ) -> runner_pb2.QuiesceResponse:
        return await self._quiesce(context, request.if_idle_since if request.HasField("if_idle_since") else None)

    @override
    async def DeliverCommand(
        self, request: runner_control_pb2.DeliverCommandRequest, context: grpc.aio.ServicerContext
    ) -> runner_control_pb2.DeliverCommandResponse:
        return runner_control_pb2.DeliverCommandResponse(
            admission=await self._deliver(request.session_id, request.command, context)
        )

    @override
    async def SubmitInput(
        self, request: runner_control_pb2.SubmitInputRequest, context: grpc.aio.ServicerContext
    ) -> runner_control_pb2.SubmitInputResponse:
        command = command_pb2.Command(
            command_id=request.command_id, submit_input=command_pb2.SubmitInput(text=request.text)
        )
        return runner_control_pb2.SubmitInputResponse(
            admission=await self._deliver(request.session_id, command, context)
        )

    @override
    async def ChangeModel(
        self, request: runner_control_pb2.ChangeModelRequest, context: grpc.aio.ServicerContext
    ) -> runner_control_pb2.ChangeModelResponse:
        command = command_pb2.Command(
            command_id=request.command_id, change_model=command_pb2.ChangeModel(model=request.model)
        )
        return runner_control_pb2.ChangeModelResponse(
            admission=await self._deliver(request.session_id, command, context)
        )

    @override
    async def ChangeReasoningEffort(
        self, request: runner_control_pb2.ChangeReasoningEffortRequest, context: grpc.aio.ServicerContext
    ) -> runner_control_pb2.ChangeReasoningEffortResponse:
        command = command_pb2.Command(
            command_id=request.command_id,
            change_reasoning_effort=command_pb2.ChangeReasoningEffort(effort=request.effort),
        )
        return runner_control_pb2.ChangeReasoningEffortResponse(
            admission=await self._deliver(request.session_id, command, context)
        )

    @override
    async def InterruptTurn(
        self, request: runner_control_pb2.InterruptTurnRequest, context: grpc.aio.ServicerContext
    ) -> runner_control_pb2.InterruptTurnResponse:
        command = command_pb2.Command(
            command_id=request.command_id, interrupt_turn=command_pb2.InterruptTurn(turn_id=request.turn_id)
        )
        return runner_control_pb2.InterruptTurnResponse(
            admission=await self._deliver(request.session_id, command, context)
        )

    @override
    async def StopRunnerSession(
        self, request: runner_control_pb2.StopRunnerSessionRequest, context: grpc.aio.ServicerContext
    ) -> runner_control_pb2.StopRunnerSessionResponse:
        command = command_pb2.Command(
            command_id=request.command_id, stop_runner_session=command_pb2.StopRunnerSession()
        )
        return runner_control_pb2.StopRunnerSessionResponse(
            admission=await self._deliver(request.session_id, command, context)
        )


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
        adopt_incarnation: Callable[[str], Awaitable[None]],
        facts: Callable[[], runner_control_pb2.RunnerFacts],
        facts_changed: asyncio.Event,
    ) -> None:
        self.runner = runner
        # Published on each connection first, then whenever `facts_changed` is set.
        self.facts = facts
        self.facts_changed = facts_changed
        # Told each Welcome's incarnation before the gateway's first call can arrive.
        self.adopt_incarnation = adopt_incarnation
        self.target = target
        # A placeholder the egress proxy swaps for this Pod's control token; the runner never holds it.
        self.bearer = bearer
        self.timing = timing
        self.boot_id = str(uuid4())

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
                if not first.welcome.incarnation_uid:
                    raise StreamLostError(None, "Welcome did not name the Sandbox incarnation UID")
                await self.adopt_incarnation(first.welcome.incarnation_uid)
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
                try:
                    await call.write(frame)
                except asyncio.InvalidStateError:
                    # A write after the gateway ended the call.
                    raise StreamLostError(await call.code(), await call.details()) from None

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
            # Cleared before reading, so a change during the read is not lost. A new connection starts
            # with none recorded, so its first heartbeat carries them even if they have not changed.
            published: runner_control_pb2.RunnerFacts | None = None
            while True:
                self.facts_changed.clear()
                if (facts := self.facts()) != published:
                    await send(
                        runner_control_pb2.RunnerFrame(heartbeat=runner_control_pb2.RunnerHeartbeat(facts=facts))
                    )
                    published = facts
                await self.facts_changed.wait()

        async def keep_alive() -> None:
            while True:
                await asyncio.sleep(self.timing.heartbeat_s)
                if loop.time() - last_frame > self.timing.liveness_s:
                    raise TimeoutError
                await send(runner_control_pb2.RunnerFrame(heartbeat=runner_control_pb2.RunnerHeartbeat()))

        try:
            async with asyncio.TaskGroup() as tasks:
                tasks.create_task(read(tasks))
                tasks.create_task(keep_alive())
                tasks.create_task(publish_facts())
        except* (grpc.aio.AioRpcError, StreamLostError, TimeoutError) as group:
            # The first failure ends the connection; the rest are its consequences.
            raise group.exceptions[0] from None
        finally:
            call.cancel()
