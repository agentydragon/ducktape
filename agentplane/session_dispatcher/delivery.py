"""Delivery: each Session's commands, oldest first, to a Sandbox whose runner is `Ready`."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from uuid import UUID

import grpc
from sqlalchemy.exc import SQLAlchemyError
from tenacity import AsyncRetrying, before_sleep_log, retry_if_exception_type, wait_exponential

from agentplane.proto import event_log_pb2, runner_pb2
from agentplane.session_dispatcher.channels import Channel
from agentplane.session_dispatcher.db import Dispatch, SessionOpening
from agentplane.session_dispatcher.ended_dispatches import EndedDispatches
from agentplane.session_dispatcher.models import DeliveryState, Operation, Reason
from agentplane.session_dispatcher.runner_client import RunnerClient
from agentplane.session_dispatcher.sandboxes import SandboxWatch, delivery_blocker
from agentplane.session_dispatcher.store import DispatchStore

# gazelle:include_dep @pypi//protobuf

logger = logging.getLogger(__name__)

# What the next attempt waits after a call that never reached a runner, doubling to the cap.
RETRY_INITIAL_S = 0.5
RETRY_MAX_S = 30


def _opening_key(session_id: str) -> str:
    """Retry state is keyed by command ID, which a Session ID never collides with."""
    return f"opening:{session_id}"


class Delivery:
    """Runs on the Dispatcher's one active replica. A command moves when its Dispatch row, its
    Sandbox or, after an unreachable runner, a backoff says it may; nothing polls."""

    def __init__(
        self, store: DispatchStore, sandboxes: SandboxWatch, runner: RunnerClient, ended: EndedDispatches
    ) -> None:
        self.ended = ended
        self.store = store
        self.sandboxes = sandboxes
        self.runner = runner
        self._changed = asyncio.Event()
        # Sessions with a delivery under way: one at a time keeps a Session's commands in order.
        self._delivering: set[str] = set()
        # The incarnation that refused work, by Sandbox UID: no retry until the Sandbox changes it.
        self._refused: dict[UUID, str | None] = {}
        self._attempts: dict[str, int] = {}
        self._retry_at: dict[str, float] = {}

    async def run(self) -> None:
        """Deliver until cancelled."""
        await self.sandboxes.wait_synced()
        await self.store.reset_delivering()
        with (
            self.store.wakeups.changes[Channel.DISPATCH].subscribe(self._changed),
            self.sandboxes.changed.subscribe(self._changed),
        ):
            async with asyncio.TaskGroup() as deliveries:
                await self._end_vanished()
                while True:
                    # Cleared before reading: a change racing with the read is a wakeup.
                    self._changed.clear()
                    try:
                        await self._end_gone()
                        await self._start_openings(deliveries)
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
        for uid in await self.store.list_sandbox_uids() - self.sandboxes.live_uids():
            await self._end(uid)

    async def _end_gone(self) -> None:
        while self.sandboxes.gone:
            uid = next(iter(self.sandboxes.gone))
            await self._end(uid)
            self.sandboxes.gone.discard(uid)

    async def _end(self, sandbox_uid: UUID) -> None:
        """Delete the Sandbox's Dispatches after ending them, and hand the ended ones to the open streams."""
        self.ended.publish(await self._retry_storage(lambda: self.store.end_sandbox(sandbox_uid)))

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
                self._delay_retry(head.command_id)
                await self._settle(
                    head, from_states=sending, to=DeliveryState.QUEUED_IN_DISPATCHER, reason=Reason.DELIVERY_UNAVAILABLE
                )
            case grpc.StatusCode.FAILED_PRECONDITION if head.operation is Operation.STOP_RUNNER_SESSION:
                await self._settle(head, from_states=sending, to=DeliveryState.NO_OP, reason=Reason.NOTHING_RUNNING)
            case code:
                await self._settle(
                    head, from_states=sending, to=DeliveryState.FAILED, reason=f"runner {code.name}: {error.details()}"
                )

    def _delay_retry(self, key: str) -> None:
        attempts = self._attempts[key] = self._attempts.get(key, 0) + 1
        self._retry_at[key] = asyncio.get_running_loop().time() + min(
            RETRY_MAX_S, RETRY_INITIAL_S * 2 ** (attempts - 1)
        )

    async def _start_openings(self, deliveries: asyncio.TaskGroup) -> None:
        now = asyncio.get_running_loop().time()
        for opening in await self.store.opening_heads():
            key = _opening_key(opening.session_id)
            if opening.session_id in self._delivering or self._retry_at.get(key, 0) > now:
                continue
            sandbox = self.sandboxes.get_sandbox(opening.sandbox_name)
            if sandbox is None or sandbox.metadata.uid != opening.sandbox_uid:
                # Its deletion reaches `_end_gone`.
                continue
            if (blocker := delivery_blocker(sandbox)) is not None:
                await self.store.note_opening_waiting(opening.session_id, blocker)
                continue
            incarnation = sandbox.status.incarnation.uid if sandbox.status.incarnation is not None else None
            if self._refused.get(sandbox.metadata.uid, "") == incarnation:
                await self.store.note_opening_waiting(opening.session_id, Reason.RUNNER_REFUSED_WORK)
                continue
            self._delivering.add(opening.session_id)
            deliveries.create_task(self._deliver_opening(opening, incarnation), name=f"open-{opening.session_id}")

    async def _deliver_opening(self, opening: SessionOpening, incarnation: str | None) -> None:
        key = _opening_key(opening.session_id)
        try:
            claimed = await self._retry_storage(
                lambda: self.store.settle_opening(
                    opening.session_id,
                    from_states={DeliveryState.QUEUED_IN_DISPATCHER},
                    to=DeliveryState.DELIVERING_TO_RUNNER,
                )
            )
            if claimed is None:
                return
            self._retry_at.pop(key, None)
            try:
                attached = await self.runner.open_session(
                    opening.sandbox_name, opening.session_id, opening.spec, opening.setup_script
                )
            except grpc.aio.AioRpcError as error:
                await self._refuse_opening(opening, incarnation, error)
                return
            self._attempts.pop(key, None)
            await self._settle_opening(opening, DeliveryState.RUNNER_ADMITTED, attached=attached)
        finally:
            self._delivering.discard(opening.session_id)
            self._changed.set()

    async def _refuse_opening(
        self, opening: SessionOpening, incarnation: str | None, error: grpc.aio.AioRpcError
    ) -> None:
        match error.code():
            case grpc.StatusCode.ABORTED:
                self._refused[opening.sandbox_uid] = incarnation
                await self._settle_opening(
                    opening, DeliveryState.QUEUED_IN_DISPATCHER, reason=Reason.RUNNER_REFUSED_WORK
                )
            case grpc.StatusCode.UNAVAILABLE | grpc.StatusCode.DEADLINE_EXCEEDED:
                self._delay_retry(_opening_key(opening.session_id))
                await self._settle_opening(
                    opening, DeliveryState.QUEUED_IN_DISPATCHER, reason=Reason.DELIVERY_UNAVAILABLE
                )
            case grpc.StatusCode.ALREADY_EXISTS:
                # An earlier attempt created it and its reply was lost, or the runner holds another spec.
                try:
                    exists = await self.runner.has_session(opening.sandbox_name, opening.session_id)
                except grpc.aio.AioRpcError as listing:
                    await self._refuse_opening(opening, incarnation, listing)
                    return
                if exists:
                    await self._settle_opening(opening, DeliveryState.RUNNER_ADMITTED)
                else:
                    await self._settle_opening(
                        opening, DeliveryState.FAILED, reason=f"runner ALREADY_EXISTS: {error.details()}"
                    )
            case code:
                await self._settle_opening(
                    opening, DeliveryState.FAILED, reason=f"runner {code.name}: {error.details()}"
                )

    async def _settle_opening(
        self,
        opening: SessionOpening,
        to: DeliveryState,
        *,
        reason: str | None = None,
        attached: runner_pb2.Attached | None = None,
    ) -> None:
        await self._retry_storage(
            lambda: self.store.settle_opening(
                opening.session_id,
                from_states={DeliveryState.DELIVERING_TO_RUNNER},
                to=to,
                reason=reason,
                attached=attached,
            )
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
        return await self._retry_storage(
            lambda: self.store.settle(
                head.command_id, from_states=from_states, to=to, reason=reason, admission=admission
            )
        )

    @staticmethod
    async def _retry_storage[T](write: Callable[[], Awaitable[T]]) -> T:
        """A write the storage outage must not lose: the call or the deletion it follows already happened."""
        async for attempt in AsyncRetrying(
            retry=retry_if_exception_type(SQLAlchemyError),
            wait=wait_exponential(max=RETRY_MAX_S),
            before_sleep=before_sleep_log(logger, logging.WARNING),
            reraise=True,
        ):
            with attempt:
                return await write()
        raise AssertionError("unreachable: AsyncRetrying without a stop retries until it returns")
