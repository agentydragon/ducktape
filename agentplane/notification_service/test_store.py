"""Committed prefixes, ownership, idempotence, replay, and delivery are distinct facts."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_bazel
from sqlalchemy import update

from agentplane.action_service.models import ActionEventView, ActionState
from agentplane.notification_service.db import Entry, Inbox, Subscription
from agentplane.notification_service.models import DestinationRef, Subscribe, SubscriptionUpdate
from agentplane.notification_service.store import ClaimLostError, ConflictError, NotFoundError, Store
from agentplane.protocol import command_pb2, event_log_pb2, event_pb2
from agentplane.subjects import ServiceAccountRef
from agentplane.workload_auth.principal import WorkloadPrincipal

# gazelle:include_dep @pypi//protobuf

PRINCIPAL = WorkloadPrincipal("test", "owner", "system:serviceaccount:test:owner", "pod", "pod-uid")
BODY = Subscribe(
    destination_ref=DestinationRef(namespace="test", name="sandbox", uid="sandbox-uid"),
    session_id="session",
    client_key="first",
    request_id=uuid4(),
)


def events(count: int = 3) -> list[ActionEventView]:
    return [
        ActionEventView(sequence=i, state=ActionState.DECISION_PENDING, at=datetime.now(UTC))
        for i in range(1, count + 1)
    ]


async def test_owner_idempotence_cancel_and_version(store: Store) -> None:
    first, second = await asyncio.gather(store.subscribe(PRINCIPAL, BODY), store.subscribe(PRINCIPAL, BODY))
    assert first == second
    with pytest.raises(ConflictError):
        await store.subscribe(PRINCIPAL, BODY.model_copy(update={"request_id": uuid4()}))
    with pytest.raises(NotFoundError):
        await store.read(ServiceAccountRef(namespace="test", name="other"), first.inbox_id, 0, 128)
    paused = await store.change(PRINCIPAL.account, first.id, SubscriptionUpdate(version=1, paused=True))
    assert paused.paused
    with pytest.raises(ConflictError):
        await store.change(PRINCIPAL.account, first.id, SubscriptionUpdate(version=1, paused=False))
    cancelled = await store.change(PRINCIPAL.account, first.id, None)
    assert cancelled.cancelled
    assert await store.change(PRINCIPAL.account, first.id, None) == cancelled


async def test_overlapping_subscriptions_commit_one_prefix_and_read_does_not_ack(store: Store) -> None:
    first = await store.subscribe(PRINCIPAL, BODY)
    await store.subscribe(PRINCIPAL, BODY.model_copy(update={"client_key": "overlap"}))
    claim = await store.claim()
    assert claim is not None
    source = await store.source(claim)
    assert source is not None
    payload = events()
    await store.record(claim, source, payload)
    overlapping = await store.source(claim)
    assert overlapping is not None
    assert overlapping.id != source.id
    await store.record(claim, overlapping, payload)
    page = await store.read(PRINCIPAL.account, first.inbox_id, 0, 128)
    assert [item.cursor for item in page.entries] == [1, 2, 3]
    assert all(len(item.subscriptions) == 2 for item in page.entries)
    assert [item.payload for item in page.entries] == [event.model_dump(mode="json") for event in payload]
    assert page.inbox.acknowledged == 0
    assert (await store.acknowledge(PRINCIPAL.account, first.inbox_id, 2)).acknowledged == 2
    assert (await store.acknowledge(PRINCIPAL.account, first.inbox_id, 1)).acknowledged == 2
    with pytest.raises(ConflictError):
        await store.acknowledge(PRINCIPAL.account, first.inbox_id, 4)
    notice = await store.notice(claim)
    assert notice is not None
    assert notice.through_cursor == 3
    retry = await store.notice(claim)
    assert retry is not None
    assert retry.command_id == notice.command_id
    await store.acknowledge(PRINCIPAL.account, first.inbox_id, 3)
    assert not await store.attempt(claim, notice)
    assert await store.notice(claim) is None


async def test_cancellation_fences_inflight_source_and_claim_loss_fences_worker(store: Store) -> None:
    subscription = await store.subscribe(PRINCIPAL, BODY)
    claim = await store.claim()
    assert claim is not None
    source = await store.source(claim)
    assert source is not None
    await store.change(PRINCIPAL.account, subscription.id, None)
    await store.record(claim, source, events())
    assert not (await store.read(PRINCIPAL.account, subscription.inbox_id, 0, 128)).entries
    async with store.sessions.begin() as session:
        await session.execute(update(Inbox).where(Inbox.id == claim.id).values(claim=uuid4()))
    with pytest.raises(ClaimLostError):
        await store.record(claim, source, events())


async def test_concurrent_sources_allocate_one_committed_prefix(store: Store) -> None:
    first = await store.subscribe(PRINCIPAL, BODY)
    second = await store.subscribe(
        PRINCIPAL, BODY.model_copy(update={"client_key": "another-action", "request_id": uuid4()})
    )
    claim = await store.claim()
    assert claim is not None
    async with store.sessions() as session:
        a = await session.get(Subscription, first.id)
        b = await session.get(Subscription, second.id)
    assert a is not None
    assert b is not None
    await asyncio.gather(store.record(claim, a, events(1)), store.record(claim, b, events(1)))
    page = await store.read(PRINCIPAL.account, first.inbox_id, 0, 128)
    assert [entry.cursor for entry in page.entries] == [1, 2]
    assert {entry.request_id for entry in page.entries} == {first.request_id, second.request_id}
    await store.retire(PRINCIPAL.account, first.inbox_id)
    assert (await store.subscription(PRINCIPAL.account, first.id)).cancelled
    with pytest.raises(ClaimLostError):
        await store.record(claim, a, events())
    # Retirement stops work without erasing payloads or acknowledging them.
    page = await store.read(PRINCIPAL.account, first.inbox_id, 0, 128)
    assert len(page.entries) == 2
    assert page.inbox.acknowledged == 0


async def test_receipts_not_ack_and_no_reminders_after_confirmation(store: Store) -> None:
    subscription = await store.subscribe(PRINCIPAL, BODY)
    claim = await store.claim()
    assert claim is not None
    source = await store.source(claim)
    assert source is not None
    await store.record(claim, source, events(1))
    notice = await store.notice(claim)
    assert notice is not None
    assert await store.attempt(claim, notice)
    admission = event_log_pb2.EventEntry(
        cursor=1,
        event=event_pb2.Event(
            command_admitted=event_pb2.CommandAdmitted(
                command=command_pb2.Command(
                    command_id=str(notice.command_id), submit_input=command_pb2.SubmitInput(text=notice.text)
                )
            )
        ),
    )
    await store.receipt(claim, notice, admission)
    page = await store.read(PRINCIPAL.account, subscription.inbox_id, 0, 128)
    assert page.notice is not None
    assert page.notice.admitted
    assert not page.notice.confirmed
    confirmation = event_log_pb2.EventEntry(
        cursor=2,
        event=event_pb2.Event(
            harness_user_message_confirmed=event_pb2.HarnessUserMessageConfirmed(
                origin_command_ids=["coalesced-other", str(notice.command_id)]
            )
        ),
    )
    await store.receipt(claim, notice, confirmation)
    await store.receipt(claim, notice, confirmation)
    page = await store.read(PRINCIPAL.account, subscription.inbox_id, 0, 128)
    assert page.notice is not None
    assert page.notice.confirmed
    assert page.inbox.acknowledged == 0
    assert await store.notice(claim) is None
    changed = event_log_pb2.EventEntry(cursor=2, event=event_pb2.Event(harness_exited=event_pb2.HarnessExited()))
    with pytest.raises(ConflictError):
        await store.receipt(claim, notice, changed)


async def test_retention_gap_is_visible_and_replay_keeps_tombstone(store: Store) -> None:
    subscription = await store.subscribe(PRINCIPAL, BODY)
    claim = await store.claim()
    assert claim is not None
    source = await store.source(claim)
    assert source is not None
    await store.record(claim, source, events())
    async with store.sessions.begin() as session:
        await session.execute(update(Entry).values(created_at=datetime.now(UTC) - timedelta(days=31)))
    await store.cleanup()
    page = await store.read(PRINCIPAL.account, subscription.inbox_id, 0, 128)
    assert page.inbox.expired_through == 3
    assert page.inbox.acknowledged == 0
    assert not page.entries
    await store.subscribe(PRINCIPAL, BODY.model_copy(update={"client_key": "late-overlap"}))
    source = await store.source(claim)
    assert source is not None
    await store.record(claim, source, events())
    assert (await store.read(PRINCIPAL.account, subscription.inbox_id, 0, 128)).inbox.last_cursor == 3


if __name__ == "__main__":
    pytest_bazel.main()
