"""Delivery: each Session's commands, oldest first, to a Sandbox whose runner is `Ready`."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from uuid import UUID

import grpc
from kubernetes_asyncio.client import ApiException
from sqlalchemy.exc import SQLAlchemyError
from tenacity import AsyncRetrying, before_sleep_log, retry_if_exception_type, wait_exponential

from agentplane.proto import event_log_pb2, runner_pb2
from agentplane.sandbox_resource import Sandbox
from agentplane.sandboxes.errors import (
    SandboxNotFoundError as InventorySandboxNotFoundError,
    SandboxQuiescingError,
    SandboxTearingDownError,
)
from agentplane.sandboxes.inventory import SandboxInventory
from agentplane.session_dispatcher.channels import Channel
from agentplane.session_dispatcher.db import Dispatch, SessionOpening
from agentplane.session_dispatcher.ended_dispatches import EndedDispatches
from agentplane.session_dispatcher.models import DeliveryState, Operation, Reason
from agentplane.session_dispatcher.ownership import LEASE_RENEW_INTERVAL_S, SandboxOwnershipLeases
from agentplane.session_dispatcher.runner_client import RunnerClient
from agentplane.session_dispatcher.sandboxes import SandboxWatch, delivery_blocker
from agentplane.session_dispatcher.store import DispatchStore, SandboxOwner

# gazelle:include_dep @pypi//protobuf

logger = logging.getLogger(__name__)

# What the next attempt waits after a call that never reached a runner, doubling to the cap.
RETRY_INITIAL_S = 0.5
RETRY_MAX_S = 30


def _opening_key(session_id: str) -> str:
    """Retry state is keyed by command ID, which a Session ID never collides with."""
    return f"opening:{session_id}"


def _incarnation_uid(sandbox: Sandbox) -> str | None:
    return None if sandbox.status.incarnation is None else str(sandbox.status.incarnation.uid)


def _awaiting_new_incarnation(sandbox: Sandbox, wake_from_incarnation: str | None) -> bool:
    """A send-triggered resume cannot deliver into the incarnation that was stopped or sealed."""
    if wake_from_incarnation is None:
        return False
    incarnation = _incarnation_uid(sandbox)
    return incarnation is None or (wake_from_incarnation != "" and incarnation == wake_from_incarnation)


class Delivery:
    """Delivers work only for Sandboxes whose Lease and DB generation this replica owns."""

    def __init__(
        self,
        store: DispatchStore,
        sandboxes: SandboxWatch,
        runner: RunnerClient,
        ended: EndedDispatches,
        inventory: SandboxInventory,
        ownership: SandboxOwnershipLeases,
    ) -> None:
        self.ended = ended
        self.store = store
        self.sandboxes = sandboxes
        self.runner = runner
        self.inventory = inventory
        self.ownership = ownership
        self._owners: dict[UUID, SandboxOwner] = {}
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
                        await self._sync_ownership()
                        await self._end_gone()
                        await self._start_openings(deliveries)
                        await self._start(deliveries)
                    except SQLAlchemyError:
                        logger.warning("dispatch storage unavailable; retrying", exc_info=True)
                        await asyncio.sleep(5)
                        continue
                    timeout = self._next_retry_in()
                    timeout = LEASE_RENEW_INTERVAL_S if timeout is None else min(timeout, LEASE_RENEW_INTERVAL_S)
                    with suppress(TimeoutError):
                        async with asyncio.timeout(timeout):
                            await self._changed.wait()

    async def _sync_ownership(self) -> None:
        """Renew live Leases, take expired ones, and recover only newly owned Sandbox work."""
        live = self.sandboxes.all_sandboxes()
        live_uids = {sandbox.metadata.uid for sandbox in live}
        for sandbox in live:
            uid = sandbox.metadata.uid
            owner = await self.ownership.acquire(sandbox.metadata.name, uid)
            if owner is None:
                self._owners.pop(uid, None)
                continue
            previous = self._owners.get(uid)
            self._owners[uid] = owner
            if previous != owner and not await self.store.recover_sandbox(owner):
                self._owners.pop(uid, None)
        for uid in self._owners.keys() - live_uids:
            self._owners.pop(uid, None)

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
        heads = await self.store.heads()
        active = {head.command_id for head in heads}
        for key in tuple(self._retry_at):
            if not key.startswith("opening:") and key not in active:
                self._clear_retry(key)
        for head in heads:
            if head.session_id in self._delivering:
                continue
            sandbox = self.sandboxes.get_sandbox(head.sandbox_name)
            if sandbox is None or sandbox.metadata.uid != head.sandbox_uid:
                # Its deletion reaches `_end_gone`.
                self._clear_retry(head.command_id)
                continue
            owner = self._owners.get(head.sandbox_uid)
            if owner is None:
                continue
            if sandbox.metadata.deletion_timestamp is not None:
                self._clear_retry(head.command_id)
                await self.store.note_waiting(head.command_id, Reason.SANDBOX_DELETED, owner=owner)
                continue
            awaiting_new_incarnation = _awaiting_new_incarnation(sandbox, head.wake_from_incarnation)
            blocker = delivery_blocker(sandbox)
            wake_disabled = (
                head.wake_eligible
                and not sandbox.spec.wake_on_send
                and (awaiting_new_incarnation or blocker is Reason.SANDBOX_SUSPENDED)
            )
            retry_at = self._retry_at.get(head.command_id)
            if retry_at is not None and retry_at > now and not wake_disabled:
                continue
            self._retry_at.pop(head.command_id, None)
            if awaiting_new_incarnation:
                if head.wake_eligible:
                    # The marker is persisted before the patch for crash safety. Reissue the guarded,
                    # idempotent patch until the desired state changes or a new incarnation appears.
                    await self._wake_for_send(head.command_id, sandbox, owner)
                else:
                    await self.store.note_waiting(head.command_id, Reason.SANDBOX_RESUMING, owner=owner)
                continue
            if blocker is not None:
                if blocker is Reason.SANDBOX_SUSPENDED and head.operation is Operation.STOP_RUNNER_SESSION:
                    self._clear_retry(head.command_id)
                    await self.store.settle(
                        head.command_id,
                        owner=owner,
                        from_states={DeliveryState.QUEUED_IN_DISPATCHER},
                        to=DeliveryState.NO_OP,
                        reason=blocker,
                    )
                elif blocker is Reason.SANDBOX_SUSPENDED and head.wake_eligible:
                    await self._wake_for_send(head.command_id, sandbox, owner)
                else:
                    await self.store.note_waiting(head.command_id, blocker, owner=owner)
                continue
            incarnation = sandbox.status.incarnation.uid if sandbox.status.incarnation is not None else None
            if self._refused.get(sandbox.metadata.uid, "") == incarnation:
                await self.store.note_waiting(head.command_id, Reason.RUNNER_REFUSED_WORK, owner=owner)
                continue
            self._delivering.add(head.session_id)
            deliveries.create_task(self._deliver(head, incarnation, owner), name=f"deliver-{head.command_id}")

    async def _deliver(self, head: Dispatch, incarnation: str | None, owner: SandboxOwner) -> None:
        try:
            claimed = await self._settle(
                head, owner, from_states={DeliveryState.QUEUED_IN_DISPATCHER}, to=DeliveryState.DELIVERING_TO_RUNNER
            )
            if claimed is None:
                return
            self._retry_at.pop(head.command_id, None)
            try:
                admission = await self.runner.deliver(head.sandbox_name, head.session_id, head.command)
            except grpc.aio.AioRpcError as error:
                await self._refuse(head, incarnation, owner, error)
                return
            self._attempts.pop(head.command_id, None)
            await self._settle(
                head,
                owner,
                from_states={DeliveryState.DELIVERING_TO_RUNNER},
                to=DeliveryState.RUNNER_ADMITTED,
                admission=admission,
            )
        finally:
            self._delivering.discard(head.session_id)
            self._changed.set()

    async def _refuse(
        self, head: Dispatch, incarnation: str | None, owner: SandboxOwner, error: grpc.aio.AioRpcError
    ) -> None:
        """What the runner's status asks of the Dispatcher (docs/adr_sandbox_session_control.md)."""
        sending = {DeliveryState.DELIVERING_TO_RUNNER}
        match error.code():
            case grpc.StatusCode.ABORTED:
                # Quiescing: delivered again once the Sandbox has resumed, which changes its incarnation.
                self._refused[head.sandbox_uid] = incarnation
                await self._settle(
                    head,
                    owner,
                    from_states=sending,
                    to=DeliveryState.QUEUED_IN_DISPATCHER,
                    reason=Reason.RUNNER_REFUSED_WORK,
                )
            case grpc.StatusCode.UNAVAILABLE | grpc.StatusCode.DEADLINE_EXCEEDED:
                self._delay_retry(head.command_id)
                await self._settle(
                    head,
                    owner,
                    from_states=sending,
                    to=DeliveryState.QUEUED_IN_DISPATCHER,
                    reason=Reason.DELIVERY_UNAVAILABLE,
                )
            case grpc.StatusCode.FAILED_PRECONDITION if head.operation is Operation.STOP_RUNNER_SESSION:
                await self._settle(
                    head, owner, from_states=sending, to=DeliveryState.NO_OP, reason=Reason.NOTHING_RUNNING
                )
            case code:
                await self._settle(
                    head,
                    owner,
                    from_states=sending,
                    to=DeliveryState.FAILED,
                    reason=f"runner {code.name}: {error.details()}",
                )

    def _delay_retry(self, key: str) -> None:
        attempts = self._attempts[key] = self._attempts.get(key, 0) + 1
        self._retry_at[key] = asyncio.get_running_loop().time() + min(
            RETRY_MAX_S, RETRY_INITIAL_S * 2 ** (attempts - 1)
        )

    async def _start_openings(self, deliveries: asyncio.TaskGroup) -> None:
        now = asyncio.get_running_loop().time()
        openings = await self.store.opening_heads()
        active = {_opening_key(opening.session_id) for opening in openings}
        for key in tuple(self._retry_at):
            if key.startswith("opening:") and key not in active:
                self._clear_retry(key)
        for opening in openings:
            key = _opening_key(opening.session_id)
            if opening.session_id in self._delivering:
                continue
            sandbox = self.sandboxes.get_sandbox(opening.sandbox_name)
            if sandbox is None or sandbox.metadata.uid != opening.sandbox_uid:
                # Its deletion reaches `_end_gone`.
                self._clear_retry(key)
                continue
            owner = self._owners.get(opening.sandbox_uid)
            if owner is None:
                continue
            if sandbox.metadata.deletion_timestamp is not None:
                self._clear_retry(key)
                await self.store.note_opening_waiting(opening.session_id, Reason.SANDBOX_DELETED, owner=owner)
                continue
            awaiting_new_incarnation = _awaiting_new_incarnation(sandbox, opening.wake_from_incarnation)
            blocker = delivery_blocker(sandbox)
            wake_disabled = (
                opening.wake_eligible
                and not sandbox.spec.wake_on_send
                and (awaiting_new_incarnation or blocker is Reason.SANDBOX_SUSPENDED)
            )
            retry_at = self._retry_at.get(key)
            if retry_at is not None and retry_at > now and not wake_disabled:
                continue
            self._retry_at.pop(key, None)
            if awaiting_new_incarnation:
                if opening.wake_eligible:
                    await self._wake_for_opening(opening, sandbox, owner)
                else:
                    await self.store.note_opening_waiting(
                        opening.session_id, Reason.SANDBOX_RESUMING, owner=owner
                    )
                continue
            if blocker is not None:
                if blocker is Reason.SANDBOX_SUSPENDED and opening.wake_eligible:
                    await self._wake_for_opening(opening, sandbox, owner)
                else:
                    await self.store.note_opening_waiting(opening.session_id, blocker, owner=owner)
                continue
            incarnation = sandbox.status.incarnation.uid if sandbox.status.incarnation is not None else None
            if self._refused.get(sandbox.metadata.uid, "") == incarnation:
                await self.store.note_opening_waiting(opening.session_id, Reason.RUNNER_REFUSED_WORK, owner=owner)
                continue
            self._delivering.add(opening.session_id)
            deliveries.create_task(
                self._deliver_opening(opening, incarnation, owner), name=f"open-{opening.session_id}"
            )

    async def _wake_for_send(self, command_id: str, sandbox: Sandbox, owner: SandboxOwner) -> None:
        if not sandbox.spec.wake_on_send:
            self._clear_retry(command_id)
            await self.store.note_waiting(command_id, Reason.WAKE_ON_SEND_DISABLED, owner=owner)
            return
        if not await self.store.note_waking(command_id, owner=owner, incarnation=_incarnation_uid(sandbox)):
            return
        await self._request_sandbox_wake(
            key=command_id,
            sandbox_name=sandbox.metadata.name,
            sandbox_uid=sandbox.metadata.uid,
            owner=owner,
            notify=lambda reason: self.store.note_waiting(command_id, reason, owner=owner),
        )

    async def _wake_for_opening(
        self, opening: SessionOpening, sandbox: Sandbox, owner: SandboxOwner
    ) -> None:
        if not sandbox.spec.wake_on_send:
            self._clear_retry(_opening_key(opening.session_id))
            await self.store.note_opening_waiting(
                opening.session_id, Reason.WAKE_ON_SEND_DISABLED, owner=owner
            )
            return
        if not await self.store.note_opening_waking(
            opening.session_id, owner=owner, incarnation=_incarnation_uid(sandbox)
        ):
            return
        await self._request_sandbox_wake(
            key=_opening_key(opening.session_id),
            sandbox_name=sandbox.metadata.name,
            sandbox_uid=sandbox.metadata.uid,
            owner=owner,
            notify=lambda reason: self.store.note_opening_waiting(opening.session_id, reason, owner=owner),
        )

    async def _request_sandbox_wake(
        self,
        *,
        key: str,
        sandbox_name: str,
        sandbox_uid: UUID,
        owner: SandboxOwner,
        notify: Callable[[Reason], Awaitable[None]],
    ) -> None:
        try:
            if self._owners.get(sandbox_uid) != owner or not await self.ownership.still_owns(owner):
                self._owners.pop(sandbox_uid, None)
                return
            if not await self.inventory.wake_for_send(sandbox_name, uid=str(sandbox_uid)):
                self._clear_retry(key)
                await notify(Reason.WAKE_ON_SEND_DISABLED)
                return
        except SandboxQuiescingError:
            self._clear_retry(key)
            await notify(Reason.SANDBOX_RESUMING)
            return
        except (SandboxTearingDownError, InventorySandboxNotFoundError):
            self._clear_retry(key)
            await notify(Reason.SANDBOX_DELETED)
            return
        except ApiException:
            self._delay_retry(key)
            logger.warning(
                "Sandbox wake request unavailable for %s; queued work will retry", sandbox_name, exc_info=True
            )
            await notify(Reason.DELIVERY_UNAVAILABLE)
            return
        self._clear_retry(key)
        await notify(Reason.SANDBOX_RESUMING)

    def _clear_retry(self, key: str) -> None:
        self._retry_at.pop(key, None)
        self._attempts.pop(key, None)

    async def _deliver_opening(
        self, opening: SessionOpening, incarnation: str | None, owner: SandboxOwner
    ) -> None:
        key = _opening_key(opening.session_id)
        try:
            claimed = await self._retry_storage(
                lambda: self.store.settle_opening(
                    opening.session_id,
                    owner=owner,
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
                await self._refuse_opening(opening, incarnation, owner, error)
                return
            self._attempts.pop(key, None)
            await self._settle_opening(opening, owner, DeliveryState.RUNNER_ADMITTED, attached=attached)
        finally:
            self._delivering.discard(opening.session_id)
            self._changed.set()

    async def _refuse_opening(
        self, opening: SessionOpening, incarnation: str | None, owner: SandboxOwner, error: grpc.aio.AioRpcError
    ) -> None:
        match error.code():
            case grpc.StatusCode.ABORTED:
                self._refused[opening.sandbox_uid] = incarnation
                await self._settle_opening(
                    opening, owner, DeliveryState.QUEUED_IN_DISPATCHER, reason=Reason.RUNNER_REFUSED_WORK
                )
            case grpc.StatusCode.UNAVAILABLE | grpc.StatusCode.DEADLINE_EXCEEDED:
                self._delay_retry(_opening_key(opening.session_id))
                await self._settle_opening(
                    opening, owner, DeliveryState.QUEUED_IN_DISPATCHER, reason=Reason.DELIVERY_UNAVAILABLE
                )
            case grpc.StatusCode.ALREADY_EXISTS:
                # An earlier attempt created it and its reply was lost, or the runner holds another spec.
                try:
                    exists = await self.runner.has_session(opening.sandbox_name, opening.session_id)
                except grpc.aio.AioRpcError as listing:
                    await self._refuse_opening(opening, incarnation, owner, listing)
                    return
                if exists:
                    await self._settle_opening(opening, owner, DeliveryState.RUNNER_ADMITTED)
                else:
                    await self._settle_opening(
                        opening, owner, DeliveryState.FAILED, reason=f"runner ALREADY_EXISTS: {error.details()}"
                    )
            case code:
                await self._settle_opening(
                    opening, owner, DeliveryState.FAILED, reason=f"runner {code.name}: {error.details()}"
                )

    async def _settle_opening(
        self,
        opening: SessionOpening,
        owner: SandboxOwner,
        to: DeliveryState,
        *,
        reason: str | None = None,
        attached: runner_pb2.Attached | None = None,
    ) -> None:
        await self._retry_storage(
            lambda: self.store.settle_opening(
                opening.session_id,
                owner=owner,
                from_states={DeliveryState.DELIVERING_TO_RUNNER},
                to=to,
                reason=reason,
                attached=attached,
            )
        )

    async def _settle(
        self,
        head: Dispatch,
        owner: SandboxOwner,
        *,
        from_states: set[DeliveryState],
        to: DeliveryState,
        reason: str | None = None,
        admission: event_log_pb2.EventEntry | None = None,
    ) -> Dispatch | None:
        return await self._retry_storage(
            lambda: self.store.settle(
                head.command_id, owner=owner, from_states=from_states, to=to, reason=reason, admission=admission
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
