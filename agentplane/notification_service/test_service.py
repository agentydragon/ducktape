"""Delivery-policy tests with real durable inbox state and worker scheduling."""

from datetime import UTC, datetime, timedelta
from typing import cast
from unittest.mock import AsyncMock, create_autospec
from uuid import uuid4

import pytest
import pytest_bazel
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncEngine

from agentplane.action_service.models import ActionEventView, ActionState
from agentplane.history_service.client import HistoryServiceClient
from agentplane.notification_service.db import Entry, Inbox, Subscription
from agentplane.notification_service.models import ActionsSource, DestinationRef, Subscribe, SubscriptionView
from agentplane.notification_service.service import Service
from agentplane.notification_service.settings import NoticeDebounceSettings, QuotaSettings
from agentplane.notification_service.sources.actions import ActionServiceClient
from agentplane.notification_service.store import ConflictError, Store
from agentplane.proto import event_log_pb2, event_pb2
from agentplane.proto.sandbox_service_pb2 import Sandbox, ServiceAccount
from agentplane.runner_gateway.client import RunnerGatewayClient
from agentplane.sandbox_service.client import SandboxServiceClient
from agentplane.sandbox_service.errors import SandboxNotFoundError
from agentplane.workload_auth.principal import WorkloadPrincipal

# gazelle:include_dep @pypi//protobuf

PRINCIPAL = WorkloadPrincipal("test", "owner", "system:serviceaccount:test:owner", "pod", "pod-uid", "account-uid")
BODY = Subscribe(
    destination_ref=DestinationRef(namespace="test", name="sandbox", uid="sandbox-uid"),
    session_id="session",
    idempotency_key="debounce",
    source=ActionsSource(provider="actions", request_id=uuid4()),
)


@pytest.fixture
async def service(store: Store) -> Service:
    return Service(
        store,
        create_autospec(ActionServiceClient),
        create_autospec(SandboxServiceClient),
        github=None,
        gateway=create_autospec(RunnerGatewayClient),
        history=create_autospec(HistoryServiceClient),
        notice_debounce=NoticeDebounceSettings(),
        stale_confirmation_s=30,
        operator_reader_account=None,
    )


def events(count: int) -> list[ActionEventView]:
    return [
        ActionEventView(sequence=i, state=ActionState.DECISION_PENDING, at=datetime.now(UTC))
        for i in range(1, count + 1)
    ]


@pytest.mark.parametrize("quiet_seconds", [0, 60])
async def test_notice_debounce_deadline_is_durable_and_reads_are_immediate(
    service: Service, engine: AsyncEngine, quiet_seconds: float
) -> None:
    store = service.store
    service.notice_debounce = NoticeDebounceSettings(quiet_seconds=quiet_seconds, max_wait_seconds=300)
    subscription = await store.subscribe(PRINCIPAL, BODY)
    claim = await store.claim()
    assert claim is not None
    source = await store.source(claim)
    assert source is not None
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
    restarted = Service(
        Store(engine, quotas=QuotaSettings()),
        service.actions,
        service.sandboxes,
        github=None,
        gateway=service.gateway,
        history=service.history,
        notice_debounce=service.notice_debounce,
        stale_confirmation_s=service.stale_confirmation_s,
        operator_reader_account=None,
    )
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
    claim = await recovered.claim()
    assert claim is not None
    if quiet_seconds:
        assert await restarted.prepare_notice(claim) is None
        async with recovered.sessions.begin() as session:
            await session.execute(update(Entry).where(Entry.cursor == 2).values(created_at=now - timedelta(seconds=61)))
    notice = await restarted.prepare_notice(claim)
    assert notice is not None
    assert notice.through_cursor == 2


async def test_notice_max_wait_and_inflight_notice_are_not_extended(service: Service) -> None:
    store = service.store
    service.notice_debounce = NoticeDebounceSettings(quiet_seconds=60, max_wait_seconds=300)
    subscription = await store.subscribe(PRINCIPAL, BODY)
    claim = await store.claim()
    assert claim is not None
    source = await store.source(claim)
    assert source is not None
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
    service.sandboxes.namespace = "test"
    cast(AsyncMock, service.actions.events).return_value = []
    subscription = await service.store.subscribe(PRINCIPAL, BODY)
    claim = await service.store.claim()
    assert claim is not None
    source = await service.store.source(claim)
    assert source is not None
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
    service: Service, engine: AsyncEngine, reason: str, prepare_notice: bool
) -> None:
    claim, subscription = await _due_inbox(service, prepare_notice=prepare_notice)
    if reason == "not_found":
        # Sandbox Service also returns NOT_FOUND when the managed label is removed.
        cast(AsyncMock, service.sandboxes.get).side_effect = SandboxNotFoundError("sandbox")
    else:
        replacement = _matching_sandbox()
        replacement.uid = "replacement-uid"
        cast(AsyncMock, service.sandboxes.get).return_value = replacement
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
    restarted = Service(
        Store(engine, quotas=QuotaSettings()),
        service.actions,
        service.sandboxes,
        github=None,
        gateway=service.gateway,
        history=service.history,
        notice_debounce=service.notice_debounce,
        stale_confirmation_s=service.stale_confirmation_s,
        operator_reader_account=None,
    )
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
    cast(AsyncMock, service.sandboxes.get).side_effect = SandboxNotFoundError("sandbox")
    assert await service.step()
    # Retry after the persisted deadline sees the *same* UID (including suspension).
    cast(AsyncMock, service.sandboxes.get).side_effect = None
    cast(AsyncMock, service.sandboxes.get).return_value = _matching_sandbox()
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
    cast(AsyncMock, service.sandboxes.get).side_effect = SandboxNotFoundError("sandbox")
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
    cast(AsyncMock, service.sandboxes.get).side_effect = TimeoutError("backend offline")
    assert await service.step()
    async with service.store.sessions() as session:
        pending = await session.get(Inbox, claim.id)
        assert pending is not None
        assert not pending.retired
        assert pending.stale_check_at is not None
        assert pending.next_attempt is not None
        assert pending.next_attempt > datetime.now(UTC)
    cast(AsyncMock, service.sandboxes.get).side_effect = None
    wrong_owner = _matching_sandbox()
    wrong_owner.service_account.name = "different-owner"
    cast(AsyncMock, service.sandboxes.get).return_value = wrong_owner
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


async def test_initial_lookup_timeout_does_not_start_stale_confirmation(service: Service) -> None:
    claim, subscription = await _due_inbox(service)
    cast(AsyncMock, service.sandboxes.get).side_effect = TimeoutError("backend offline")
    assert await service.step()
    async with service.store.sessions() as session:
        inbox = await session.get(Inbox, claim.id)
        source = await session.get(Subscription, subscription.id)
        assert inbox is not None
        assert inbox.stale_check_at is None
        assert not inbox.retired
        assert source is not None
        assert not source.cancelled


if __name__ == "__main__":
    pytest_bazel.main()
