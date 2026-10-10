"""The runner's peer of the runner channel: it dials the Sandbox Service, keeps one `Connect` stream,
admits the commands delivered over it and serves each journal follow the service asks for on its own
`Follow` RPC. Contract: `agentplane/docs/runner_channel.md`."""

from __future__ import annotations

import asyncio
import logging
from contextlib import aclosing
from dataclasses import dataclass
from uuid import uuid4

import grpc
from google.protobuf import empty_pb2
from sqlalchemy.exc import SQLAlchemyError
from tenacity import AsyncRetrying, RetryCallState, retry_if_exception_type, wait_random_exponential

from agentplane.grpc_options import grpc_channel_option_kvps
from agentplane.protocol import command_pb2
from agentplane.runner.journal import CommandConflictError, JournalSealedError
from agentplane.runner.service import OpenError, Runner, build_attached, finish_command, follow_journal
from agentplane.runner_channel import protocol_pb2 as channel_pb2, protocol_pb2_grpc

# gazelle:include_dep @pypi//protobuf

logger = logging.getLogger(__name__)

CAPABILITIES = ("commands", "events")
# Past this many follows on one connection, the runner refuses the next with `REASON_FOLLOW_LIMIT`.
FOLLOW_LIMIT = 64
EPOCH_METADATA = "x-agentplane-connection-epoch"
FOLLOW_METADATA = "x-agentplane-follow-id"

type Metadata = tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class ChannelTiming:
    heartbeat_s: float = 10
    liveness_s: float = 30
    backoff_initial_s: float = 0.5
    backoff_max_s: float = 30


class ChannelLostError(Exception):
    """A connection ended before it carried anything; the next one waits out the backoff."""

    def __init__(self, code: grpc.StatusCode | None, detail: str) -> None:
        super().__init__(f"{code=} {detail}")
        self.code = code


def _refused(
    deliver: channel_pb2.DeliverCommand, reason: channel_pb2.CommandRefused.Reason, detail: str
) -> channel_pb2.CommandReceipt:
    return channel_pb2.CommandReceipt(
        session_id=deliver.session_id,
        command_id=deliver.command.command_id,
        refused=channel_pb2.CommandRefused(reason=reason, detail=detail),
    )


async def receipt(runner: Runner, deliver: channel_pb2.DeliverCommand) -> channel_pb2.CommandReceipt:
    """Admit a delivered command and report the journal's answer. A command already journaled with
    the same payload is answered with its existing entry, so redelivery is safe."""
    command = deliver.command
    if not command.command_id or command.WhichOneof("operation") is None:
        return _refused(deliver, channel_pb2.CommandRefused.REASON_INVALID_COMMAND, "command_id and operation required")
    session = await runner.stored_session(deliver.session_id)
    if session is None:
        return _refused(deliver, channel_pb2.CommandRefused.REASON_SESSION_UNKNOWN, "no such session")
    stored = await session.journal.get(command.command_id)
    if stored is None:
        # Like Attach, delivery never starts a stopped harness.
        if not session.running:
            return _refused(deliver, channel_pb2.CommandRefused.REASON_HARNESS_STOPPED, "the harness is not running")
        try:
            await finish_command(session.command(command))
        except CommandConflictError as error:
            return _refused(deliver, channel_pb2.CommandRefused.REASON_COMMAND_CONFLICT, str(error))
        except JournalSealedError:
            return _refused(deliver, channel_pb2.CommandRefused.REASON_HARNESS_STOPPED, "the session is sealed")
    elif command_pb2.Command.FromString(stored.payload) != command:
        return _refused(
            deliver, channel_pb2.CommandRefused.REASON_COMMAND_CONFLICT, "the command id was admitted for other work"
        )
    return channel_pb2.CommandReceipt(
        session_id=deliver.session_id,
        command_id=command.command_id,
        admitted=await session.journal.admitted_entry(command.command_id),
    )


def _follow_refused(reason: channel_pb2.FollowRefused.Reason, detail: str) -> channel_pb2.FollowFrame:
    return channel_pb2.FollowFrame(refused=channel_pb2.FollowRefused(reason=reason, detail=detail))


async def stream_follow(
    runner: Runner,
    call: grpc.aio.StreamUnaryCall[channel_pb2.FollowFrame, empty_pb2.Empty],
    opened: channel_pb2.OpenFollow,
) -> None:
    """Write one Session's journal after `opened.after_cursor`, then its live entries, until it is
    sealed. It only reads: it never starts a harness, and cancelling it interrupts no turn."""
    session = await runner.stored_session(opened.session_id)
    if session is None:
        await call.write(_follow_refused(channel_pb2.FollowRefused.REASON_SESSION_UNKNOWN, "no such session"))
    elif opened.after_cursor > (published := session.journal.recovery_state).through_cursor:
        await call.write(
            _follow_refused(
                channel_pb2.FollowRefused.REASON_CURSOR_BEYOND_JOURNAL,
                f"after_cursor {opened.after_cursor} is beyond the journal, "
                f"whose last cursor is {published.through_cursor}",
            )
        )
    else:
        await call.write(channel_pb2.FollowFrame(attached=build_attached(session, published)))
        cursor = opened.after_cursor
        async with aclosing(follow_journal(session.journal, cursor)) as entries:
            async for entry in entries:
                await call.write(channel_pb2.FollowFrame(entry=entry))
                cursor = entry.cursor
        await call.write(channel_pb2.FollowFrame(sealed=cursor))
    await call.done_writing()
    await call


def _report_follow(task: asyncio.Task[None]) -> None:
    if not task.cancelled() and (error := task.exception()) is not None:
        logger.error("%s failed", task.get_name(), exc_info=error)


class RunnerChannelClient:
    """Keeps one `Connect` stream to the Sandbox Service for as long as the runner runs.

    Each connection dials a fresh HTTP/2 connection from a local subchannel pool: gRPC's global pool
    would hand a reconnect the stale connection that just failed.
    """

    def __init__(
        self,
        runner: Runner,
        *,
        target: str,
        bearer: str,
        timing: ChannelTiming,
        channel_options: dict[str, int | str] | None = None,
    ) -> None:
        self.runner = runner
        self.target = target
        # A placeholder the egress proxy swaps for this Pod's channel token; the runner never holds it.
        self.bearer = bearer
        self.timing = timing
        self.channel_options = channel_options
        self.boot_id = str(uuid4())

    def _backoff(self, state: RetryCallState) -> float:
        error = state.outcome.exception() if state.outcome is not None else None
        if isinstance(error, ChannelLostError) and error.code == grpc.StatusCode.PERMISSION_DENIED:
            return self.timing.backoff_max_s
        return wait_random_exponential(multiplier=self.timing.backoff_initial_s, max=self.timing.backoff_max_s)(state)

    def _metadata(self) -> Metadata:
        return (("authorization", f"Bearer {self.bearer}"),)

    async def follow(
        self, stub: protocol_pb2_grpc.RunnerChannelAsyncStub, metadata: Metadata, opened: channel_pb2.OpenFollow
    ) -> None:
        call = stub.Follow(metadata=metadata)
        try:
            await stream_follow(self.runner, call, opened)
        except grpc.aio.AioRpcError, asyncio.InvalidStateError:
            if not call.done():
                raise
            # The service ended the follow: a transport event, never a command.
            logger.info("follow %s ended by the service: %s", opened.follow_id, await call.code())
        finally:
            call.cancel()

    @staticmethod
    async def refuse_follow(stub: protocol_pb2_grpc.RunnerChannelAsyncStub, metadata: Metadata) -> None:
        call = stub.Follow(metadata=metadata)
        try:
            await call.write(
                _follow_refused(channel_pb2.FollowRefused.REASON_FOLLOW_LIMIT, f"at most {FOLLOW_LIMIT} follows")
            )
            await call.done_writing()
            await call
        finally:
            call.cancel()

    async def run(self) -> None:
        while True:
            # A connection that carried traffic returns normally and resets the backoff; one that
            # ended before that raises, so a connection superseded straight away cannot spin.
            async for attempt in AsyncRetrying(
                retry=retry_if_exception_type(ChannelLostError),
                wait=self._backoff,
                before_sleep=lambda state: logger.warning(
                    "runner channel lost: %s", state.outcome.exception() if state.outcome is not None else None
                ),
            ):
                with attempt:
                    await self.connect()

    async def connect(self) -> None:
        loop = asyncio.get_running_loop()
        channel = grpc.aio.insecure_channel(
            self.target,
            options=grpc_channel_option_kvps(self.channel_options, required={"grpc.use_local_subchannel_pool": 1}),
        )
        welcomed_at: float | None = None
        lost = ChannelLostError(None, "the stream ended")
        try:
            stub = protocol_pb2_grpc.RunnerChannelStub(channel)
            call = stub.Connect(metadata=self._metadata())
            await call.write(
                channel_pb2.RunnerFrame(hello=channel_pb2.Hello(runner_boot_id=self.boot_id, capabilities=CAPABILITIES))
            )
            async with asyncio.timeout(self.timing.liveness_s):
                first = await call.read()
            if not isinstance(first, channel_pb2.ServiceFrame):
                raise ChannelLostError(await call.code(), await call.details())
            if first.WhichOneof("frame") != "welcome":
                raise ChannelLostError(None, f"expected Welcome, got {first.WhichOneof('frame')}")
            welcomed_at = loop.time()
            logger.info("runner channel connected at epoch %d", first.welcome.connection_epoch)
            await self.serve(stub, call, first.welcome)
        except grpc.aio.AioRpcError as error:
            lost = ChannelLostError(error.code(), error.details() or "")
        except TimeoutError:
            lost = ChannelLostError(None, "no frame from the Sandbox Service")
        except ChannelLostError as error:
            lost = error
        finally:
            await channel.close()
        if welcomed_at is not None and loop.time() - welcomed_at >= self.timing.liveness_s:
            logger.warning("runner channel ended: %s", lost)
            return
        raise lost

    async def serve(
        self,
        stub: protocol_pb2_grpc.RunnerChannelAsyncStub,
        call: grpc.aio.StreamStreamCall[channel_pb2.RunnerFrame, channel_pb2.ServiceFrame],
        welcome: channel_pb2.Welcome,
    ) -> None:
        """Answer deliveries and follows and keep the stream alive until it ends; always raises. The
        connection's follows end with it."""
        loop = asyncio.get_running_loop()
        last_frame = loop.time()
        writing = asyncio.Lock()
        commands = "commands" in welcome.capabilities
        events = "events" in welcome.capabilities
        follows: dict[str, asyncio.Task[None]] = {}
        refusals: set[asyncio.Task[None]] = set()

        async def send(frame: channel_pb2.RunnerFrame) -> None:
            async with writing:
                await call.write(frame)

        async def answer(deliver: channel_pb2.DeliverCommand) -> None:
            try:
                answered = await receipt(self.runner, deliver)
            except OpenError, OSError, SQLAlchemyError, ValueError:
                # No receipt is not a refusal: the service's attempt expires and the caller retries.
                logger.exception("session %s: cannot answer command %s", deliver.session_id, deliver.command.command_id)
                return
            await send(channel_pb2.RunnerFrame(receipt=answered))

        def open_follow(opened: channel_pb2.OpenFollow) -> None:
            if opened.follow_id in follows:
                raise ChannelLostError(grpc.StatusCode.INVALID_ARGUMENT, f"follow {opened.follow_id} is already open")
            metadata = (
                *self._metadata(),
                (EPOCH_METADATA, str(welcome.connection_epoch)),
                (FOLLOW_METADATA, opened.follow_id),
            )
            name = f"follow {opened.follow_id} of session {opened.session_id}"
            if len(follows) >= FOLLOW_LIMIT:
                task = asyncio.create_task(self.refuse_follow(stub, metadata), name=f"refused {name}")
                refusals.add(task)
                task.add_done_callback(refusals.discard)
            else:
                task = asyncio.create_task(self.follow(stub, metadata, opened), name=name)
                follows[opened.follow_id] = task
                task.add_done_callback(lambda _: follows.pop(opened.follow_id, None))
            task.add_done_callback(_report_follow)

        async def read(tasks: asyncio.TaskGroup) -> None:
            nonlocal last_frame
            while isinstance(frame := await call.read(), channel_pb2.ServiceFrame):
                last_frame = loop.time()
                match frame.WhichOneof("frame"):
                    case "heartbeat":
                        pass
                    case "deliver" if commands:
                        tasks.create_task(answer(frame.deliver))
                    case "open_follow" if events:
                        open_follow(frame.open_follow)
                    case "close_follow" if events:
                        # A follow that already ended has nothing left to close.
                        if (closed := follows.get(frame.close_follow.follow_id)) is not None:
                            closed.cancel()
                    case kind:
                        raise ChannelLostError(grpc.StatusCode.INVALID_ARGUMENT, f"unexpected {kind} frame")
            raise ChannelLostError(await call.code(), await call.details())

        async def keep_alive() -> None:
            while True:
                await asyncio.sleep(self.timing.heartbeat_s)
                if loop.time() - last_frame > self.timing.liveness_s:
                    raise TimeoutError
                await send(channel_pb2.RunnerFrame(heartbeat=channel_pb2.Heartbeat()))

        try:
            async with asyncio.TaskGroup() as tasks:
                tasks.create_task(read(tasks))
                tasks.create_task(keep_alive())
        except* (grpc.aio.AioRpcError, ChannelLostError, TimeoutError) as group:
            # The first failure ends the connection; the rest are its consequences.
            raise group.exceptions[0] from None
        finally:
            call.cancel()
            # Already reported by `_report_follow`.
            ending = [*follows.values(), *refusals]
            for task in ending:
                task.cancel()
            await asyncio.gather(*ending, return_exceptions=True)
