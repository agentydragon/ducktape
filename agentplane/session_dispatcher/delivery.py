"""Delivery: each Session's commands, oldest first, to a Sandbox whose runner is `Ready`."""

import asyncio
import logging
from contextlib import suppress
from uuid import UUID

import grpc
from sqlalchemy.exc import SQLAlchemyError
from tenacity import AsyncRetrying, before_sleep_log, retry_if_exception_type, wait_exponential

from agentplane.proto import event_log_pb2
from agentplane.session_dispatcher.channels import Channel
from agentplane.session_dispatcher.db import Dispatch
from agentplane.session_dispatcher.models import DeliveryState, Operation, Reason
from agentplane.session_dispatcher.runner_client import RunnerClient
from agentplane.session_dispatcher.sandboxes import SandboxWatch, delivery_blocker
from agentplane.session_dispatcher.store import DispatchStore

# gazelle:include_dep @pypi//protobuf

logger = logging.getLogger(__name__)

# What the next attempt waits after a call that never reached a runner, doubling to the cap.
RETRY_INITIAL_S = 0.5
RETRY_MAX_S = 30


class Delivery:
    """Runs on the Dispatcher's one active replica. A command moves when its Dispatch row, its
    Sandbox or, after an unreachable runner, a backoff says it may; nothing polls."""

    def __init__(self, store: DispatchStore, sandboxes: SandboxWatch, runner: RunnerClient) -> None:
        self.store = store
        self.sandboxes = sandboxes
        self.runner = runner
        self._changed = asyncio.Event()
        # Sessions with a delivery under way: one at a time keeps a Session's commands in order.
        self._delivering: set[str] = set()
        # The incarnation that refused work, by Sandbox UID: no retry until the Sandbox changes it.
        self._refused: dict[UUID | None, str | None] = {}
        self._attempts: dict[str, int] = {}
        self._retry_at: dict[str, float] = {}

    async def run(self) -> None:
        """Deliver until cancelled."""
        await self.sandboxes.wait_synced()
        await self.store.reset_delivering()
        await self._end_vanished()
        with (
            self.store.wakeups.changes[Channel.DISPATCH].subscribe(self._changed),
            self.sandboxes.changed.subscribe(self._changed),
        ):
            async with asyncio.TaskGroup() as deliveries:
                while True:
                    # Cleared before reading: a change racing with the read is a wakeup.
                    self._changed.clear()
                    try:
                        await self._end_gone()
                        await self._start(deliveries)
                    except SQLAlchemyError:
                        logger.warning("dispatch storage unavailable; retrying", exc_info=True)
                        await asyncio.sleep(5)
                        continue
                    with suppress(TimeoutError):
                        async with asyncio.timeout(self._next_retry_in()):
                            await self._changed.wait()

    async def _end_vanished(self) -> None:
        """Sandboxes deleted while this process was down: the first listing is complete."""
        for uid in await self.store.unfinished_sandbox_uids() - self.sandboxes.live_uids():
            await self.store.end_sandbox(uid)

    async def _end_gone(self) -> None:
        while self.sandboxes.gone:
            uid = next(iter(self.sandboxes.gone))
            await self.store.end_sandbox(uid)
            self.sandboxes.gone.discard(uid)

    def _next_retry_in(self) -> float | None:
        if not self._retry_at:
            return None
        return max(0.0, min(self._retry_at.values()) - asyncio.get_running_loop().time())

    async def _start(self, deliveries: asyncio.TaskGroup) -> None:
        now = asyncio.get_running_loop().time()
        for head in await self.store.heads():
            if head.session_id in self._delivering or self._retry_at.get(head.command_id, 0) > now:
                continue
            sandbox = self.sandboxes.get_sandbox(head.sandbox_name)
            if sandbox is None or sandbox.metadata.uid != head.sandbox_uid:
                # Its deletion reaches `_end_gone`.
                continue
            if (blocker := delivery_blocker(sandbox)) is not None:
                if blocker is Reason.SANDBOX_SUSPENDED and head.operation is Operation.STOP_RUNNER_SESSION:
                    await self.store.settle(
                        head.command_id,
                        from_states={DeliveryState.QUEUED_IN_DISPATCHER},
                        to=DeliveryState.NO_OP,
                        reason=blocker,
                    )
                else:
                    await self.store.note_waiting(head.command_id, blocker)
                continue
            incarnation = sandbox.status.incarnation.uid if sandbox.status.incarnation is not None else None
            if self._refused.get(sandbox.metadata.uid, "") == incarnation:
                await self.store.note_waiting(head.command_id, Reason.RUNNER_REFUSED_WORK)
                continue
            self._delivering.add(head.session_id)
            deliveries.create_task(self._deliver(head, incarnation), name=f"deliver-{head.command_id}")

    async def _deliver(self, head: Dispatch, incarnation: str | None) -> None:
        try:
            claimed = await self._settle(
                head, from_states={DeliveryState.QUEUED_IN_DISPATCHER}, to=DeliveryState.DELIVERING_TO_RUNNER
            )
            if claimed is None:
                return
            self._retry_at.pop(head.command_id, None)
            try:
                admission = await self.runner.deliver(head.sandbox_name, head.session_id, head.command)
            except grpc.aio.AioRpcError as error:
                await self._refuse(head, incarnation, error)
                return
            self._attempts.pop(head.command_id, None)
            await self._settle(
                head,
                from_states={DeliveryState.DELIVERING_TO_RUNNER},
                to=DeliveryState.RUNNER_ADMITTED,
                admission=admission,
            )
        finally:
            self._delivering.discard(head.session_id)
            self._changed.set()

    async def _refuse(self, head: Dispatch, incarnation: str | None, error: grpc.aio.AioRpcError) -> None:
        """What the runner's status asks of the Dispatcher (docs/adr_sandbox_session_control.md)."""
        sending = {DeliveryState.DELIVERING_TO_RUNNER}
        match error.code():
            case grpc.StatusCode.ABORTED:
                # Quiescing: delivered again once the Sandbox has resumed, which changes its incarnation.
                self._refused[head.sandbox_uid] = incarnation
                await self._settle(
                    head, from_states=sending, to=DeliveryState.QUEUED_IN_DISPATCHER, reason=Reason.RUNNER_REFUSED_WORK
                )
            case grpc.StatusCode.UNAVAILABLE | grpc.StatusCode.DEADLINE_EXCEEDED:
                attempts = self._attempts[head.command_id] = self._attempts.get(head.command_id, 0) + 1
                self._retry_at[head.command_id] = asyncio.get_running_loop().time() + min(
                    RETRY_MAX_S, RETRY_INITIAL_S * 2 ** (attempts - 1)
                )
                await self._settle(
                    head, from_states=sending, to=DeliveryState.QUEUED_IN_DISPATCHER, reason=Reason.DELIVERY_UNAVAILABLE
                )
            case grpc.StatusCode.FAILED_PRECONDITION if head.operation is Operation.STOP_RUNNER_SESSION:
                await self._settle(head, from_states=sending, to=DeliveryState.NO_OP, reason=Reason.NOTHING_RUNNING)
            case code:
                await self._settle(
                    head, from_states=sending, to=DeliveryState.FAILED, reason=f"runner {code.name}: {error.details()}"
                )

    async def _settle(
        self,
        head: Dispatch,
        *,
        from_states: set[DeliveryState],
        to: DeliveryState,
        reason: str | None = None,
        admission: event_log_pb2.EventEntry | None = None,
    ) -> Dispatch | None:
        """A transition the storage outage must not lose: the call it follows already happened."""
        async for attempt in AsyncRetrying(
            retry=retry_if_exception_type(SQLAlchemyError),
            wait=wait_exponential(max=RETRY_MAX_S),
            before_sleep=before_sleep_log(logger, logging.WARNING),
            reraise=True,
        ):
            with attempt:
                return await self.store.settle(
                    head.command_id, from_states=from_states, to=to, reason=reason, admission=admission
                )
        raise AssertionError("unreachable: AsyncRetrying without a stop retries until it returns")
