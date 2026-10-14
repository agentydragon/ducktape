"""Delivery-policy tests with real durable inbox state and worker scheduling."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock
from uuid import uuid4

import aiohttp
import grpc
import pytest
import pytest_bazel
from kubernetes_asyncio import client as k8s_client
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncEngine

from agentplane.action_service.models import ActionEventView, ActionState
from agentplane.grpc_calls import ServiceError
from agentplane.notification_service.db import Entry, Inbox, Notice, Subscription
from agentplane.notification_service.dispatcher_client import SessionDispatcherClient
from agentplane.notification_service.models import ActionsSource, DestinationRef, Subscribe, SubscriptionView
from agentplane.notification_service.service import Service
from agentplane.notification_service.settings import NoticeDebounceSettings, QuotaSettings
from agentplane.notification_service.store import ConflictError, Store
from agentplane.notification_service.testing.store import claimed
from agentplane.proto import event_log_pb2, event_pb2, history_service_pb2, runner_pb2, session_dispatcher_pb2
from agentplane.proto.sandboxes_pb2 import Sandbox, SandboxDestination, ServiceAccount
from agentplane.runner_gateway.client import SandboxRunner
from agentplane.sandboxes.errors import SandboxNotFoundError
from agentplane.workload_auth.principal import WorkloadPrincipal

# gazelle:include_dep @pypi//protobuf

PRINCIPAL = WorkloadPrincipal("test", "owner", "system:serviceaccount:test:owner", "pod", "pod-uid", "account-uid")
BODY = Subscribe(
    destination_ref=DestinationRef(namespace="test", name="sandbox", uid="sandbox-uid"),
    session_id="session",
    idempotency_key="debounce",
    source=ActionsSource(provider="actions", request_id=uuid4()),
)


def events(count: int) -> list[ActionEventView]:
    return [
        ActionEventView(sequence=i, state=ActionState.DECISION_PENDING, at=datetime.now(UTC))
        for i in range(1, count + 1)
    ]


@pytest.mark.parametrize("quiet_seconds", [0, 60])
async def test_notice_debounce_deadline_is_durable_and_reads_are_immediate(
    service: Service, restart: Callable[[Service], Service], quiet_seconds: float
) -> None:
    store = service.store
    service.notice_debounce = NoticeDebounceSettings(quiet_seconds=quiet_seconds, max_wait_seconds=300)
    subscription = await store.subscribe(PRINCIPAL, BODY)
    claim, source = await claimed(store)
    await store.record(claim, source, events(2))
    now = datetime.now(UTC)
    first, last = now - timedelta(seconds=120), now - timedelta(seconds=1)
    async with store.sessions.begin() as session:
        await session.execute(update(Entry).where(Entry.cursor == 1).values(created_at=first))
        await session.execute(update(Entry).where(Entry.cursor == 2).values(created_at=last))
    # Matching the same events through another subscription must not extend the quiet window.
    overlap = await store.subscribe(PRINCIPAL, BODY.model_copy(update={"idempotency_key": "overlap"}))
    other_source = await store.source(claim)
    assert other_source is not None
    assert other_source.id == overlap.id
    await store.record(claim, other_source, events(2))
    page = await store.read(PRINCIPAL.account, subscription.inbox_id, 0, 128)
    assert [entry.cursor for entry in page.entries] == [1, 2]
    assert page.inbox.acknowledged == 0
    assert page.notice is None
    # Remove source deadlines, so only notice delivery can schedule this inbox.
    await store.change(PRINCIPAL.account, subscription.id, None)
    await store.change(PRINCIPAL.account, overlap.id, None)
    await store.release(claim, None, notice_due_at=await service.get_notice_due_at(claim))
    restarted = restart(service)
    recovered = restarted.store
    async with recovered.sessions() as session:
        inbox = await session.get(Inbox, claim.id)
        assert inbox is not None
        assert inbox.next_attempt == last + timedelta(seconds=quiet_seconds)
    if quiet_seconds:
        assert await recovered.get_next_work_at() == last + timedelta(seconds=quiet_seconds)
        assert await recovered.claim() is None
    # Age just the quiet window; recovery needs no process-local timer or new NOTIFY.
    async with recovered.sessions.begin() as session:
        await session.execute(update(Inbox).where(Inbox.id == claim.id).values(next_attempt=now))
    reclaimed = await recovered.claim()
    assert reclaimed is not None
    if quiet_seconds:
        assert await restarted.prepare_notice(reclaimed) is None
        async with recovered.sessions.begin() as session:
            await session.execute(update(Entry).where(Entry.cursor == 2).values(created_at=now - timedelta(seconds=61)))
    notice = await restarted.prepare_notice(reclaimed)
    assert notice is not None
    assert notice.through_cursor == 2


async def test_notice_max_wait_and_inflight_notice_are_not_extended(service: Service) -> None:
    store = service.store
    service.notice_debounce = NoticeDebounceSettings(quiet_seconds=60, max_wait_seconds=300)
    subscription = await store.subscribe(PRINCIPAL, BODY)
    claim, source = await claimed(store)
    await store.record(claim, source, events(2))
    assert await service.prepare_notice(claim) is None
    # A recent entry cannot postpone an older pending entry beyond the maximum wait.
    async with store.sessions.begin() as session:
        await session.execute(
            update(Entry).where(Entry.cursor == 1).values(created_at=datetime.now(UTC) - timedelta(seconds=301))
        )
    notice = await service.prepare_notice(claim)
    assert notice is not None
    assert notice.through_cursor == 2
    await store.record(claim, source, events(3))
    retry = await service.prepare_notice(claim)
    assert retry is not None
    assert (retry.command_id, retry.text, retry.through_cursor) == (notice.command_id, notice.text, 2)
    await store.receipt(
        claim,
        notice,
        event_log_pb2.EventEntry(
            cursor=1,
            event=event_pb2.Event(
                harness_user_message_confirmed=event_pb2.HarnessUserMessageConfirmed(
                    origin_command_ids=[str(notice.command_id)]
                )
            ),
        ),
        after_cursor=0,
    )
    # The next burst has its own window; the already covered old entry cannot force it due.
    assert await service.prepare_notice(claim) is None
    await store.change(PRINCIPAL.account, subscription.id, None)
    await store.acknowledge(PRINCIPAL.account, subscription.inbox_id, 3)
    await store.release(claim, None, notice_due_at=await service.get_notice_due_at(claim))
    assert await store.get_next_work_at() is None


def _matching_sandbox() -> Sandbox:
    # No Pod needed: a suspended Sandbox retains its identity.
    return Sandbox(
        name=BODY.destination_ref.name,
        uid=BODY.destination_ref.uid,
        service_account=ServiceAccount(namespace="test", name="owner"),
    )


async def _due_inbox(service: Service, *, prepare_notice: bool = False) -> tuple[Inbox, SubscriptionView]:
    service.notice_debounce = NoticeDebounceSettings(quiet_seconds=3600, max_wait_seconds=3600)
    cast(AsyncMock, service.actions.events).return_value = []
    subscription = await service.store.subscribe(PRINCIPAL, BODY)
    claim, source = await claimed(service.store)
    await service.store.record(claim, source, events(3))
    if prepare_notice:
        assert await service.store.notice(claim, prepare=True) is not None
    await service.store.release(claim, None)
    async with service.store.sessions.begin() as session:
        await session.execute(
            update(Inbox).where(Inbox.id == claim.id).values(next_attempt=datetime.now(UTC) - timedelta(seconds=1))
        )
    return claim, subscription


@pytest.mark.parametrize("reason", ["not_found", "uid_changed"])
@pytest.mark.parametrize("prepare_notice", [False, True])
async def test_stale_inbox_retires_after_persisted_recheck(
    service: Service, engine: AsyncEngine, restart: Callable[[Service], Service], reason: str, prepare_notice: bool
) -> None:
    claim, subscription = await _due_inbox(service, prepare_notice=prepare_notice)
    if reason == "not_found":
        cast(AsyncMock, service.sandboxes.get_without_pod).side_effect = SandboxNotFoundError("sandbox")
    else:
        replacement = _matching_sandbox()
        replacement.uid = "replacement-uid"
        cast(AsyncMock, service.sandboxes.get_without_pod).return_value = replacement
    assert await service.step()
    async with service.store.sessions() as session:
        pending = await session.get(Inbox, claim.id)
        assert pending is not None
        assert not pending.retired
        assert pending.stale_check_at is not None
        assert pending.next_attempt == pending.stale_check_at
        assert pending.delivery_error == f"destination pending retirement: {reason}"
    # A second replica cannot claim the inbox before the durable confirmation deadline.
    assert await Store(engine, quotas=QuotaSettings()).claim() is None
    async with service.store.sessions.begin() as session:
        await session.execute(
            update(Inbox)
            .where(Inbox.id == claim.id)
            .values(
                stale_check_at=datetime.now(UTC) - timedelta(seconds=1),
                next_attempt=datetime.now(UTC) - timedelta(seconds=1),
            )
        )
    restarted = restart(service)
    assert await restarted.step()
    async with service.store.sessions() as session:
        retired = await session.get(Inbox, claim.id)
        row = await session.get(Subscription, subscription.id)
        assert retired is not None
        assert retired.retired
        assert retired.next_attempt is None
        assert retired.acknowledged == 0
        assert retired.last_cursor == 3
        assert retired.delivery_error == f"destination retired: {reason}"
        assert row is not None
        assert row.cancelled
    page = await service.store.read(PRINCIPAL.account, subscription.inbox_id, 0, 128)
    assert [entry.cursor for entry in page.entries] == [1, 2, 3]
    assert (page.notice is not None) == prepare_notice
    assert await Store(engine, quotas=QuotaSettings()).claim() is None
    with pytest.raises(ConflictError, match="retired"):
        await service.store.subscribe(PRINCIPAL, BODY.model_copy(update={"idempotency_key": "new"}))


async def test_transient_missing_destination_can_recover_without_retirement(service: Service) -> None:
    claim, subscription = await _due_inbox(service)
    cast(AsyncMock, service.sandboxes.get_without_pod).side_effect = SandboxNotFoundError("sandbox")
    assert await service.step()
    # Retry after the persisted deadline sees the *same* UID (including suspension).
    cast(AsyncMock, service.sandboxes.get_without_pod).side_effect = None
    cast(AsyncMock, service.sandboxes.get_without_pod).return_value = _matching_sandbox()
    async with service.store.sessions.begin() as session:
        await session.execute(
            update(Inbox).where(Inbox.id == claim.id).values(next_attempt=datetime.now(UTC) - timedelta(seconds=1))
        )
    assert await service.step()
    async with service.store.sessions() as session:
        recovered = await session.get(Inbox, claim.id)
        assert recovered is not None
        assert not recovered.retired
        assert recovered.stale_check_at is None
        assert recovered.delivery_error is None
        row = await session.get(Subscription, subscription.id)
        assert row is not None
        assert not row.cancelled


async def test_transient_lookup_and_owner_mismatch_cannot_confirm_staleness(service: Service) -> None:
    claim, subscription = await _due_inbox(service)
    cast(AsyncMock, service.sandboxes.get_without_pod).side_effect = SandboxNotFoundError("sandbox")
    assert await service.step()
    async with service.store.sessions.begin() as session:
        await session.execute(
            update(Inbox)
            .where(Inbox.id == claim.id)
            .values(
                stale_check_at=datetime.now(UTC) - timedelta(seconds=1),
                next_attempt=datetime.now(UTC) - timedelta(seconds=1),
            )
        )
    cast(AsyncMock, service.sandboxes.get_without_pod).side_effect = TimeoutError("backend offline")
    assert await service.step()
    async with service.store.sessions() as session:
        pending = await session.get(Inbox, claim.id)
        assert pending is not None
        assert not pending.retired
        assert pending.stale_check_at is not None
        assert pending.next_attempt is not None
        assert pending.next_attempt > datetime.now(UTC)
    cast(AsyncMock, service.sandboxes.get_without_pod).side_effect = None
    wrong_owner = _matching_sandbox()
    wrong_owner.service_account.name = "different-owner"
    cast(AsyncMock, service.sandboxes.get_without_pod).return_value = wrong_owner
    async with service.store.sessions.begin() as session:
        await session.execute(
            update(Inbox).where(Inbox.id == claim.id).values(next_attempt=datetime.now(UTC) - timedelta(seconds=1))
        )
    assert await service.step()
    async with service.store.sessions() as session:
        pending = await session.get(Inbox, claim.id)
        source = await session.get(Subscription, subscription.id)
        assert pending is not None
        assert not pending.retired
        assert pending.stale_check_at is None
        assert pending.delivery_error == "OwnerMismatchError"
        assert source is not None
        assert not source.cancelled


@pytest.mark.parametrize(
    "failure",
    [TimeoutError("backend offline"), k8s_client.ApiException(status=503), aiohttp.ClientConnectionError("refused")],
)
async def test_initial_lookup_failure_does_not_start_stale_confirmation(service: Service, failure: Exception) -> None:
    claim, subscription = await _due_inbox(service)
    cast(AsyncMock, service.sandboxes.get_without_pod).side_effect = failure
    assert await service.step()
    async with service.store.sessions() as session:
        inbox = await session.get(Inbox, claim.id)
        source = await session.get(Subscription, subscription.id)
        assert inbox is not None
        assert inbox.stale_check_at is None
        assert not inbox.retired
        assert source is not None
        assert not source.cancelled


async def _dispatcher_notice(service: Service) -> tuple[Inbox, SubscriptionView, Notice]:
    subscription = await service.store.subscribe(PRINCIPAL, BODY)
    claim, source = await claimed(service.store)
    await service.store.record(claim, source, events(1))
    notice = await service.store.notice(claim, prepare=True)
    assert notice is not None
    cast(AsyncMock, service.history.read_session_events).return_value = history_service_pb2.ReadSessionEventsResponse(
        last_cursor=0
    )
    return claim, subscription, notice


def _sandbox_runner(harness_state: str = "HARNESS_STATE_RUNNING") -> SandboxRunner:
    destination = SandboxDestination(
        sandbox=BODY.destination_ref.name,
        sandbox_uid=BODY.destination_ref.uid,
        owner=ServiceAccount(namespace=PRINCIPAL.namespace, name=PRINCIPAL.service_account_name),
    )
    return cast(
        SandboxRunner,
        SimpleNamespace(
            destination=destination,
            list_sessions=AsyncMock(
                return_value=[runner_pb2.SessionSummary(session_id=BODY.session_id, harness_state=harness_state)]
            ),
        ),
    )


async def test_ack_before_dispatch_suppresses_notice_even_when_harness_is_stopped(service: Service) -> None:
    claim, subscription, notice = await _dispatcher_notice(service)
    runner = _sandbox_runner("HARNESS_STATE_STOPPED")
    await service.store.acknowledge(PRINCIPAL.account, subscription.inbox_id, notice.through_cursor)

    await service.deliver(claim, runner, notice)

    page = await service.store.read(PRINCIPAL.account, subscription.inbox_id, 0, 128)
    assert page.notice is not None
    assert not page.notice.attempted
    assert page.notice.error == "suppressed: acknowledged before submission"
    cast(AsyncMock, runner.list_sessions).assert_not_awaited()
    cast(AsyncMock, service.dispatcher.submit_input).assert_not_awaited()
    cast(AsyncMock, service.gateway.command).assert_not_awaited()


async def test_dispatcher_retries_keep_the_notice_id_and_queued_is_not_admitted(service: Service) -> None:
    claim, subscription, notice = await _dispatcher_notice(service)
    dispatcher = cast(SessionDispatcherClient, service.dispatcher)
    submit = cast(AsyncMock, dispatcher.submit_input)
    runner = _sandbox_runner("HARNESS_STATE_STOPPED")
    submit.return_value = session_dispatcher_pb2.Dispatch(
        state=session_dispatcher_pb2.DELIVERY_STATE_QUEUED_IN_DISPATCHER
    )

    await service.deliver(claim, runner, notice)
    await service.deliver(claim, runner, notice)

    assert submit.await_count == 2
    cast(AsyncMock, runner.list_sessions).assert_not_awaited()
    assert [call.kwargs for call in submit.await_args_list] == [
        {
            "sandbox": BODY.destination_ref.name,
            "session_id": BODY.session_id,
            "command_id": str(notice.command_id),
            "text": notice.text,
        }
    ] * 2
    page = await service.store.read(PRINCIPAL.account, subscription.inbox_id, 0, 128)
    assert page.notice is not None
    assert page.notice.attempted
    assert not page.notice.admitted
    assert not page.notice.confirmed
    assert page.notice.error is None
    cast(AsyncMock, service.gateway.command).assert_not_awaited()


async def test_dispatcher_terminal_outcome_is_persisted_without_gateway_fallback(service: Service) -> None:
    claim, subscription, notice = await _dispatcher_notice(service)
    dispatcher = cast(SessionDispatcherClient, service.dispatcher)
    cast(AsyncMock, dispatcher.submit_input).return_value = session_dispatcher_pb2.Dispatch(
        state=session_dispatcher_pb2.DELIVERY_STATE_FAILED, reason="sandbox_deleted"
    )

    await service.deliver(claim, _sandbox_runner(), notice)

    page = await service.store.read(PRINCIPAL.account, subscription.inbox_id, 0, 128)
    assert page.notice is not None
    assert page.notice.error == "Dispatcher DELIVERY_STATE_FAILED: sandbox_deleted"
    cast(AsyncMock, service.gateway.command).assert_not_awaited()


async def test_dispatcher_authentication_failure_never_falls_back_to_gateway(service: Service) -> None:
    claim, _, notice = await _dispatcher_notice(service)
    dispatcher = cast(SessionDispatcherClient, service.dispatcher)
    cast(AsyncMock, dispatcher.submit_input).side_effect = ServiceError(
        grpc.StatusCode.UNAUTHENTICATED, "unauthenticated"
    )

    with pytest.raises(ServiceError, match="unauthenticated"):
        await service.deliver(claim, _sandbox_runner(), notice)
    cast(AsyncMock, service.gateway.command).assert_not_awaited()


if __name__ == "__main__":
    pytest_bazel.main()
