"""The runner's peer of the runner control stream: it dials the Runner Gateway, keeps one `Connect` stream,
admits the commands delivered over it and answers its lifecycle requests. Contract:
`agentplane/docs/runner_control.md`."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from uuid import uuid4

import grpc
from sqlalchemy.exc import SQLAlchemyError

from agentplane.proto import command_pb2, runner_control_pb2, runner_control_pb2_grpc, runner_pb2
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


def _refused(
    deliver: runner_control_pb2.DeliverCommand, reason: runner_control_pb2.CommandRefused.Reason, detail: str
) -> runner_control_pb2.CommandReceipt:
    return runner_control_pb2.CommandReceipt(
        session_id=deliver.session_id,
        command_id=deliver.command.command_id,
        refused=runner_control_pb2.CommandRefused(reason=reason, detail=detail),
    )


async def receipt(runner: Runner, deliver: runner_control_pb2.DeliverCommand) -> runner_control_pb2.CommandReceipt:
    """Admit a delivered command and report the journal's answer. A command already journaled with
    the same payload is answered with its existing entry, so redelivery is safe."""
    command = deliver.command
    if not command.command_id or command.WhichOneof("operation") is None:
        return _refused(
            deliver, runner_control_pb2.CommandRefused.REASON_INVALID_COMMAND, "command_id and operation required"
        )
    session = await runner.stored_session(deliver.session_id)
    if session is None:
        return _refused(deliver, runner_control_pb2.CommandRefused.REASON_SESSION_UNKNOWN, "no such session")
    stored = await session.journal.get(command.command_id)
    if stored is None:
        # Delivery never starts a stopped harness.
        if not session.running:
            return _refused(
                deliver, runner_control_pb2.CommandRefused.REASON_HARNESS_STOPPED, "the harness is not running"
            )
        try:
            await finish_command(session.command(command))
        except CommandConflictError as error:
            return _refused(deliver, runner_control_pb2.CommandRefused.REASON_COMMAND_CONFLICT, str(error))
        except JournalSealedError:
            return _refused(deliver, runner_control_pb2.CommandRefused.REASON_HARNESS_STOPPED, "the session is sealed")
    elif command_pb2.Command.FromString(stored.payload) != command:
        return _refused(
            deliver,
            runner_control_pb2.CommandRefused.REASON_COMMAND_CONFLICT,
            "the command id was admitted for other work",
        )
    return runner_control_pb2.CommandReceipt(
        session_id=deliver.session_id,
        command_id=command.command_id,
        admitted=await session.journal.admitted_entry(command.command_id),
    )


def _attached(session: Session) -> runner_pb2.Attached:
    return build_attached(session, session.journal.recovery_state)


def _control_refused(
    request: runner_control_pb2.ControlRequest, reason: runner_control_pb2.ControlRefused.Reason, detail: str
) -> runner_control_pb2.ControlResult:
    return runner_control_pb2.ControlResult(
        request_id=request.request_id, refused=runner_control_pb2.ControlRefused(reason=reason, detail=detail)
    )


async def control(runner: Runner, request: runner_control_pb2.ControlRequest) -> runner_control_pb2.ControlResult:
    """Carry out one lifecycle request and always answer it. Initialization is answered with its
    terminal event only, once the attempt has ended."""
    result = runner_control_pb2.ControlResult(request_id=request.request_id)
    try:
        match request.WhichOneof("verb"):
            case "list_sessions":
                result.list_sessions.sessions.extend(runner.summaries())
            case "initialize":
                try:
                    log, execution = await runner.initialize(request.initialize.script)
                except ValueError as error:
                    raise InvalidRequestError(str(error)) from error
                if execution is not None:
                    # The bootstrap belongs to the Sandbox: abandoning this request must not stop it.
                    await asyncio.shield(execution)
                result.initialize.CopyFrom(log.since(log.last_sequence - 1)[0])
            case "open" if not request.open.HasField("spec"):
                raise InvalidRequestError("open.spec is required")
            case "open":
                opened = request.open
                setup_script = opened.setup_script if opened.HasField("setup_script") else None
                result.open.CopyFrom(_attached(await runner.open_session(opened.session_id, opened.spec, setup_script)))
            case "resume":
                result.resume.CopyFrom(_attached(await runner.resume(request.resume.session_id)))
            case "seal":
                result.seal.CopyFrom(await runner.seal())
            case None:
                raise InvalidRequestError("the request names no verb")
    except SessionUnknownError as error:
        return _control_refused(request, runner_control_pb2.ControlRefused.REASON_SESSION_UNKNOWN, str(error))
    except SpecConflictError as error:
        return _control_refused(request, runner_control_pb2.ControlRefused.REASON_SPEC_CONFLICT, str(error))
    except (SandboxSealedError, JournalSealedError) as error:
        return _control_refused(request, runner_control_pb2.ControlRefused.REASON_SEALED, str(error))
    except SetupFailedError as error:
        return _control_refused(request, runner_control_pb2.ControlRefused.REASON_SETUP_FAILED, str(error))
    except InvalidRequestError as error:
        return _control_refused(request, runner_control_pb2.ControlRefused.REASON_INVALID_REQUEST, str(error))
    except InitializationConflictError as error:
        return _control_refused(request, runner_control_pb2.ControlRefused.REASON_BOOTSTRAP_CONFLICT, str(error))
    except Exception as error:  # answered with the failure, such as a harness that did not launch, not left to hang
        logger.exception("control request %s failed", request.request_id)
        return _control_refused(request, runner_control_pb2.ControlRefused.REASON_FAILED, str(error))
    return result


class RunnerControlClient:
    """Keeps one `Connect` stream to the Runner Gateway for as long as the runner runs.

    Each connection dials a fresh HTTP/2 connection from a local subchannel pool: gRPC's global pool
    would hand a reconnect the stale connection that just failed.
    """

    def __init__(self, runner: Runner, *, target: str, bearer: str, timing: StreamTiming) -> None:
        self.runner = runner
        self.target = target
        # A placeholder the egress proxy swaps for this Pod's control token; the runner never holds it.
        self.bearer = bearer
        self.timing = timing
        self.boot_id = str(uuid4())

    async def run(self) -> None:
        async def attempt() -> bool:
            # A connection that carried traffic returns and resets the backoff.
            await self.connect()
            return False

        await reconnect(attempt, timing=self.timing, operator_codes=OPERATOR_CODES, describe="runner control stream")

    async def connect(self) -> None:
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
                welcomed_at = loop.time()
                logger.info("runner control stream connected at epoch %d", first.welcome.connection_epoch)
                await self.serve(call)
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
        self, call: grpc.aio.StreamStreamCall[runner_control_pb2.RunnerFrame, runner_control_pb2.ServiceFrame]
    ) -> None:
        """Answer deliveries and control requests and keep the stream alive until it ends; always
        raises. The connection's unanswered requests go unanswered: the service asks again."""
        loop = asyncio.get_running_loop()
        last_frame = loop.time()
        writing = asyncio.Lock()
        # Frames on `Connect` are ordered, and so is Seal against the work around it: a Seal waits
        # for the admissions of every command delivered before it, and each delivery or control
        # request after it waits for the Seal. Everything else runs concurrently.
        admissions: set[asyncio.Task[None]] = set()
        sealing: asyncio.Task[None] | None = None

        async def send(frame: runner_control_pb2.RunnerFrame) -> None:
            async with writing:
                await call.write(frame)

        async def after(preceding: Iterable[asyncio.Task[None]]) -> None:
            # Ordering only: a preceding task's outcome is its own to report.
            if pending := set(preceding):
                await asyncio.wait(pending)

        async def respond(request: runner_control_pb2.ControlRequest, preceding: Iterable[asyncio.Task[None]]) -> None:
            await after(preceding)
            operation = control(self.runner, request)
            # A lost connection abandons the result, not a half-done operation, which a re-asked
            # OpenSession would only confirm. Initialization protects its own bootstrap, which may run
            # for long after the connection is gone.
            answered = await (operation if request.WhichOneof("verb") == "initialize" else finish_command(operation))
            await send(runner_control_pb2.RunnerFrame(control_result=answered))

        def handle_control(tasks: asyncio.TaskGroup, request: runner_control_pb2.ControlRequest) -> None:
            nonlocal sealing
            if request.WhichOneof("verb") == "seal":
                sealing = tasks.create_task(respond(request, set(admissions)))
            else:
                tasks.create_task(respond(request, () if sealing is None else (sealing,)))

        def admit(tasks: asyncio.TaskGroup, deliver: runner_control_pb2.DeliverCommand) -> None:
            task = tasks.create_task(answer(deliver, () if sealing is None else (sealing,)))
            admissions.add(task)
            task.add_done_callback(admissions.discard)

        async def answer(deliver: runner_control_pb2.DeliverCommand, preceding: Iterable[asyncio.Task[None]]) -> None:
            await after(preceding)
            try:
                answered = await receipt(self.runner, deliver)
            except OpenError, OSError, SQLAlchemyError, ValueError:
                # No receipt is not a refusal: the service's attempt expires and the caller retries.
                logger.exception("session %s: cannot answer command %s", deliver.session_id, deliver.command.command_id)
                return
            await send(runner_control_pb2.RunnerFrame(receipt=answered))

        async def read(tasks: asyncio.TaskGroup) -> None:
            nonlocal last_frame
            while isinstance(frame := await call.read(), runner_control_pb2.ServiceFrame):
                last_frame = loop.time()
                match frame.WhichOneof("frame"):
                    case "heartbeat":
                        pass
                    case "deliver":
                        admit(tasks, frame.deliver)
                    case "control":
                        handle_control(tasks, frame.control)
                    case kind:
                        raise StreamLostError(grpc.StatusCode.INVALID_ARGUMENT, f"unexpected {kind} frame")
            raise StreamLostError(await call.code(), await call.details())

        async def keep_alive() -> None:
            while True:
                await asyncio.sleep(self.timing.heartbeat_s)
                if loop.time() - last_frame > self.timing.liveness_s:
                    raise TimeoutError
                await send(runner_control_pb2.RunnerFrame(heartbeat=runner_control_pb2.Heartbeat()))

        try:
            async with asyncio.TaskGroup() as tasks:
                tasks.create_task(read(tasks))
                tasks.create_task(keep_alive())
        except* (grpc.aio.AioRpcError, StreamLostError, TimeoutError) as group:
            # The first failure ends the connection; the rest are its consequences.
            raise group.exceptions[0] from None
        finally:
            call.cancel()
