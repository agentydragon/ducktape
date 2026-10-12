"""The runner's push of each Session's journal to its ingress, one `Push` call per Session over one
HTTP/2 connection. Contract: `agentplane/docs/runner_control.md` § Event ingress."""

from __future__ import annotations

import asyncio
import logging
from contextlib import AsyncExitStack, aclosing

import grpc

from agentplane.proto import runner_ingress_pb2, runner_ingress_pb2_grpc, runner_pb2
from agentplane.runner.reconnect import StreamLostError, StreamTiming, reconnect
from agentplane.runner.runner import Runner, build_attached, follow_journal
from agentplane.runner.session import Session
from agentplane.runner.store import IngressAcknowledgement, SessionStore

# gazelle:include_dep @pypi//protobuf

logger = logging.getLogger(__name__)

# Entries a call sends beyond the latest acknowledgement before it waits for more.
IN_FLIGHT = 1024
# Only an operator resolves these, so they back off at the maximum and are logged as errors.
OPERATOR_CODES = frozenset({grpc.StatusCode.PERMISSION_DENIED, grpc.StatusCode.FAILED_PRECONDITION})


class _Push:
    """One `Push` call for one Session."""

    def __init__(
        self,
        call: grpc.aio.StreamStreamCall[runner_ingress_pb2.PushFrame, runner_ingress_pb2.Acknowledgement],
        session: Session,
        store: SessionStore,
        timing: StreamTiming,
    ) -> None:
        self.call = call
        self.session = session
        self.store = store
        self.timing = timing
        self.recorded = store.read_acknowledgement(session.session_id)
        self.acknowledged = 0
        self.sent = 0
        # Set, and replaced, whenever the acknowledged cursor advances.
        self.advanced = asyncio.Event()
        self.progressed = False
        self.sealed_sent = False
        self.writing = asyncio.Lock()
        loop = asyncio.get_running_loop()
        self.last_sent = self.last_received = loop.time()

    async def write(self, frame: runner_ingress_pb2.PushFrame) -> None:
        async with self.writing:
            await self.call.write(frame)
            self.last_sent = asyncio.get_running_loop().time()

    def record(self, acknowledgement: runner_ingress_pb2.Acknowledgement) -> None:
        if acknowledgement.cursor < self.acknowledged or acknowledgement.cursor > self.sent:
            raise StreamLostError(
                grpc.StatusCode.FAILED_PRECONDITION,
                f"the ingress acknowledged cursor {acknowledgement.cursor} after {self.acknowledged}, "
                f"having been sent through {self.sent}",
            )
        if acknowledgement.cursor > self.acknowledged:
            self.acknowledged = acknowledgement.cursor
            advanced, self.advanced = self.advanced, asyncio.Event()
            advanced.set()
        latest = IngressAcknowledgement(cursor=acknowledgement.cursor, sealed=acknowledgement.sealed)
        if latest != self.recorded:
            self.store.write_acknowledgement(self.session.session_id, latest)
            self.recorded = latest

    async def run(self) -> None:
        """Push until the ingress acknowledges the journal's seal."""
        journal = self.session.journal
        await self.write(runner_ingress_pb2.PushFrame(announce=build_attached(self.session, journal.recovery_state)))
        async with asyncio.timeout(self.timing.liveness_s):
            first = await self.call.read()
        if not isinstance(first, runner_ingress_pb2.Acknowledgement):
            raise StreamLostError(await self.call.code(), await self.call.details())
        # The ingress then holds entries this journal never wrote.
        if first.cursor > journal.last_cursor:
            raise StreamLostError(
                grpc.StatusCode.FAILED_PRECONDITION,
                f"the ingress acknowledged cursor {first.cursor} beyond the journal's last cursor {journal.last_cursor}",
            )
        self.sent = first.cursor
        self.record(first)
        if first.sealed:
            return
        try:
            async with asyncio.TaskGroup() as tasks:
                others = [tasks.create_task(self.send()), tasks.create_task(self.keep_alive())]
                await self.receive()
                for task in others:
                    task.cancel()
        except* (grpc.aio.AioRpcError, asyncio.InvalidStateError, StreamLostError) as group:
            # The first failure ends the call; the rest are its consequences.
            raise group.exceptions[0] from None

    async def send(self) -> None:
        """The journal after the first acknowledgement, replayed and then live, then its seal."""
        cursor = self.sent
        async with aclosing(follow_journal(self.session.journal, cursor)) as entries:
            async for entry in entries:
                while entry.cursor > self.acknowledged + IN_FLIGHT:
                    await self.advanced.wait()
                self.sent = entry.cursor
                await self.write(runner_ingress_pb2.PushFrame(entry=entry))
                cursor = entry.cursor
        self.sealed_sent = True
        await self.write(runner_ingress_pb2.PushFrame(sealed=runner_pb2.Sealed(cursor=cursor)))
        async with self.writing:
            await self.call.done_writing()

    async def receive(self) -> None:
        loop = asyncio.get_running_loop()
        while isinstance(acknowledgement := await self.call.read(), runner_ingress_pb2.Acknowledgement):
            self.last_received = loop.time()
            before = self.acknowledged
            self.record(acknowledgement)
            self.progressed |= self.acknowledged > before
            if acknowledgement.sealed:
                return
        raise StreamLostError(await self.call.code(), await self.call.details())

    async def keep_alive(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            await asyncio.sleep(self.timing.heartbeat_s)
            if loop.time() - self.last_received > self.timing.liveness_s:
                raise StreamLostError(None, "no frame from the ingress")
            if not self.sealed_sent and loop.time() - self.last_sent >= self.timing.heartbeat_s:
                await self.write(runner_ingress_pb2.PushFrame(heartbeat=runner_ingress_pb2.Heartbeat()))


def _report_push(task: asyncio.Task[None]) -> None:
    if not task.cancelled() and (error := task.exception()) is not None:
        logger.error("%s failed", task.get_name(), exc_info=error)


class IngressPusher:
    """Pushes every Session whose seal the ingress has not acknowledged, for as long as the runner
    runs. Commands never wait on it: the journal is the buffer."""

    def __init__(self, runner: Runner, *, target: str, bearer: str, timing: StreamTiming) -> None:
        self.runner = runner
        self.target = target
        # A placeholder the egress proxy swaps for this Pod's ingress token; the runner never holds it.
        self.bearer = bearer
        self.timing = timing
        # Each Session's push, ending once the ingress acknowledged its seal.
        self.pushes: dict[str, asyncio.Task[None]] = {}

    async def run(self) -> None:
        async with AsyncExitStack() as stack:
            # The ingress's own reconnects follow the control stream's backoff, not gRPC's default 120 s cap.
            channel = await stack.enter_async_context(
                grpc.aio.insecure_channel(
                    self.target,
                    options=(
                        ("grpc.initial_reconnect_backoff_ms", int(self.timing.backoff_initial_s * 1000)),
                        ("grpc.max_reconnect_backoff_ms", int(self.timing.backoff_max_s * 1000)),
                    ),
                )
            )
            stub = runner_ingress_pb2_grpc.RunnerIngressStub(channel)

            def track(session: Session) -> None:
                acknowledged = self.runner.store.read_acknowledgement(session.session_id)
                if session.session_id in self.pushes or (acknowledged is not None and acknowledged.sealed):
                    return
                task = asyncio.create_task(self.push(stub, session), name=f"push of session {session.session_id}")
                task.add_done_callback(_report_push)
                self.pushes[session.session_id] = task

            stack.push_async_callback(self.cancel_pushes)
            self.runner.session_loaded.append(track)
            stack.callback(self.runner.session_loaded.remove, track)
            for session_id in self.runner.store.session_ids():
                if (session := await self.runner.stored_session(session_id)) is not None:
                    track(session)
            await asyncio.Future()

    async def cancel_pushes(self) -> None:
        for task in self.pushes.values():
            task.cancel()
        await asyncio.gather(*self.pushes.values(), return_exceptions=True)

    async def push(self, stub: runner_ingress_pb2_grpc.RunnerIngressAsyncStub, session: Session) -> None:
        # A call that got anything new acknowledged returns False and resets the backoff.
        await reconnect(
            lambda: self.push_once(stub, session),
            timing=self.timing,
            operator_codes=OPERATOR_CODES,
            describe=f"session {session.session_id}: push to the ingress",
        )

    async def push_once(self, stub: runner_ingress_pb2_grpc.RunnerIngressAsyncStub, session: Session) -> bool:
        """True once the ingress acknowledged the seal; False after a call that made progress."""
        call = stub.Push(metadata=(("authorization", f"Bearer {self.bearer}"),))
        push = _Push(call, session, self.runner.store, self.timing)
        try:
            await push.run()
            return True
        except grpc.aio.AioRpcError as error:
            lost = StreamLostError(error.code(), error.details() or "")
        except TimeoutError:
            lost = StreamLostError(None, "no acknowledgement from the ingress")
        except asyncio.InvalidStateError:
            # A write after the ingress ended the call.
            lost = StreamLostError(await call.code(), await call.details())
        except StreamLostError as error:
            lost = error
        finally:
            call.cancel()
        if push.progressed and lost.code not in OPERATOR_CODES:
            logger.warning("session %s: push to the ingress ended: %s", session.session_id, lost)
            return False
        raise lost
