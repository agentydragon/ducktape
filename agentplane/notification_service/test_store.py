"""Committed prefixes, ownership, idempotence, replay, and delivery are distinct facts."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_bazel
from alembic import command
from alembic.config import Config
from sqlalchemy import text, update
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine

from agentplane.action_service.models import ActionEventView, ActionState
from agentplane.notification_service.database_migrate import RUNNER
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
    idempotency_key="first",
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
    assert first.idempotency_key == BODY.idempotency_key
    assert "client_key" not in first.model_dump()
    async with store.sessions() as session:
        row = await session.get(Subscription, first.id)
        assert row is not None
        assert row.creation == BODY.model_dump(mode="json")
    # The key is local to the session inbox, not the whole sandbox or account.
    other_session = await store.subscribe(PRINCIPAL, BODY.model_copy(update={"session_id": "other-session"}))
    assert other_session.inbox_id != first.inbox_id
    assert other_session.id != first.id
    assert other_session.idempotency_key == first.idempotency_key
    with pytest.raises(ConflictError):
        await store.subscribe(PRINCIPAL, BODY.model_copy(update={"request_id": uuid4()}))
    with pytest.raises(NotFoundError):
        await store.read(ServiceAccountRef(namespace="test", name="other"), first.inbox_id, 0, 128)
    before_renewal = datetime.now(UTC)
    renewed = await store.change(PRINCIPAL.account, first.id, SubscriptionUpdate(version=1, lifetime_days=30))
    assert renewed.version == 2
    assert before_renewal + timedelta(days=30) <= renewed.expires_at <= datetime.now(UTC) + timedelta(days=30)
    with pytest.raises(ConflictError):
        await store.change(PRINCIPAL.account, first.id, SubscriptionUpdate(version=1))
    cancelled = await store.change(PRINCIPAL.account, first.id, None)
    assert cancelled.cancelled
    assert await store.change(PRINCIPAL.account, first.id, None) == cancelled
    assert await store.subscribe(PRINCIPAL, BODY) == cancelled
    with pytest.raises(ConflictError):
        await store.change(PRINCIPAL.account, first.id, SubscriptionUpdate(version=cancelled.version))


async def test_idempotency_key_migration_preserves_populated_inbox(store: Store, engine: AsyncEngine) -> None:
    subscription = await store.subscribe(PRINCIPAL, BODY)
    claim = await store.claim()
    assert claim is not None
    source = await store.source(claim)
    assert source is not None
    await store.record(claim, source, events())
    notice = await store.notice(claim)
    assert notice is not None
    assert await store.attempt(claim, notice)
    await store.acknowledge(PRINCIPAL.account, subscription.inbox_id, 1)
    before = await store.read(PRINCIPAL.account, subscription.inbox_id, 0, 128)

    def round_trip(connection: Connection) -> None:
        config = Config()
        config.set_main_option("script_location", str(RUNNER.migrations_dir))
        config.attributes["connection"] = connection
        command.downgrade(config, "0001_notifications")
        legacy = BODY.model_dump(mode="json")
        legacy["client_key"] = legacy.pop("idempotency_key")
        row = connection.execute(
            text("SELECT client_key, creation FROM subscription WHERE id = :id"), {"id": subscription.id}
        ).one()
        assert row[0] == BODY.idempotency_key
        assert row[1] == legacy
        RUNNER.run_for_connection(connection)
        # Reapplying the image-owned chain is harmless and verifies ORM/schema agreement.
        RUNNER.run_for_connection(connection)

    async with engine.begin() as connection:
        await connection.run_sync(round_trip)
    replayed = await store.subscribe(PRINCIPAL, BODY)
    assert replayed.id == subscription.id
    assert replayed.idempotency_key == BODY.idempotency_key
    assert replayed.after_sequence == 3
    assert await store.read(PRINCIPAL.account, subscription.inbox_id, 0, 128) == before


@pytest.mark.parametrize("paused", [False, True])
@pytest.mark.parametrize("cancelled", [False, True])
async def test_remove_pause_migration_preserves_inbox_and_stopped_intent(
    store: Store, engine: AsyncEngine, paused: bool, cancelled: bool
) -> None:
    subscription = await store.subscribe(PRINCIPAL, BODY)
    claim = await store.claim()
    assert claim is not None
    source = await store.source(claim)
    assert source is not None
    await store.record(claim, source, events())
    notice = await store.notice(claim)
    assert notice is not None
    assert await store.attempt(claim, notice)
    await store.acknowledge(PRINCIPAL.account, subscription.inbox_id, 1)
    if cancelled:
        await store.change(PRINCIPAL.account, subscription.id, None)
    before = await store.subscription(PRINCIPAL.account, subscription.id)
    inbox_before = await store.read(PRINCIPAL.account, subscription.inbox_id, 0, 128)

    def round_trip(connection: Connection) -> None:
        config = Config()
        config.set_main_option("script_location", str(RUNNER.migrations_dir))
        config.attributes["connection"] = connection
        command.downgrade(config, "0002_idempotency_key")
        connection.execute(
            text("UPDATE subscription SET paused = :paused WHERE id = :id"), {"id": subscription.id, "paused": paused}
        )
        RUNNER.run_for_connection(connection)
        RUNNER.run_for_connection(connection)
        # Rollback preserves the cancellation rather than restoring a resumable state.
        command.downgrade(config, "0002_idempotency_key")
        row = connection.execute(
            text("SELECT paused, cancelled FROM subscription WHERE id = :id"), {"id": subscription.id}
        ).one()
        assert row == (False, paused or cancelled)
        RUNNER.run_for_connection(connection)

    async with engine.begin() as connection:
        await connection.run_sync(round_trip)
    expected = before.model_copy(
        update={"cancelled": paused or cancelled, "version": before.version + int(paused and not cancelled)}
    )
    assert await store.subscribe(PRINCIPAL, BODY) == expected
    assert await store.read(PRINCIPAL.account, subscription.inbox_id, 0, 128) == inbox_before
    async with store.sessions.begin() as session:
        await session.execute(update(Subscription).values(next_poll=datetime.now(UTC) - timedelta(seconds=1)))
    current = await store.source(claim)
    if paused or cancelled:
        assert current is None
        # An in-flight fetch from before cancellation cannot append more events.
        await store.record(claim, source, events(4))
        assert await store.read(PRINCIPAL.account, subscription.inbox_id, 0, 128) == inbox_before
    else:
        assert current is not None
        await store.record(claim, current, events(4))
        page = await store.read(PRINCIPAL.account, subscription.inbox_id, 0, 128)
        assert page.inbox.last_cursor == 4
        assert page.inbox.acknowledged == 1


async def test_overlapping_subscriptions_commit_one_prefix_and_read_does_not_ack(store: Store) -> None:
    first = await store.subscribe(PRINCIPAL, BODY)
    await store.subscribe(PRINCIPAL, BODY.model_copy(update={"idempotency_key": "overlap"}))
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
        PRINCIPAL, BODY.model_copy(update={"idempotency_key": "another-action", "request_id": uuid4()})
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


@pytest.mark.parametrize("baseline", [0, 100_000])
async def test_receipts_not_ack_and_no_reminders_after_confirmation(store: Store, baseline: int) -> None:
    subscription = await store.subscribe(PRINCIPAL, BODY)
    claim = await store.claim()
    assert claim is not None
    source = await store.source(claim)
    assert source is not None
    await store.record(claim, source, events(1))
    notice = await store.notice(claim)
    assert notice is not None
    boundary = event_log_pb2.EventEntry(cursor=baseline)
    if baseline:
        # Unattempted notices, including ones partially replayed by the old worker, can skip
        # unrelated history. Recovery checks the exact persisted boundary before continuing.
        await store.receipt(claim, notice, event_log_pb2.EventEntry(cursor=1))
        await store.checkpoint_before_attempt(claim, notice, boundary)
        await store.receipt(claim, notice, boundary)
        with pytest.raises(ConflictError):
            await store.checkpoint_before_attempt(claim, notice, boundary)
        persisted = await store.notice(claim, prepare=False)
        assert persisted is not None
        assert persisted.runner_cursor == baseline
        assert not persisted.attempted
    assert await store.attempt(claim, notice)
    with pytest.raises(ConflictError):
        await store.checkpoint_before_attempt(claim, notice, event_log_pb2.EventEntry(cursor=baseline + 100))
    with pytest.raises(ConflictError):
        await store.receipt(claim, notice, event_log_pb2.EventEntry(cursor=baseline + 2))
    admission = event_log_pb2.EventEntry(
        cursor=baseline + 1,
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
        cursor=baseline + 2,
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
    changed = event_log_pb2.EventEntry(
        cursor=baseline + 2, event=event_pb2.Event(harness_exited=event_pb2.HarnessExited())
    )
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
    await store.subscribe(PRINCIPAL, BODY.model_copy(update={"idempotency_key": "late-overlap"}))
    source = await store.source(claim)
    assert source is not None
    await store.record(claim, source, events())
    assert (await store.read(PRINCIPAL.account, subscription.inbox_id, 0, 128)).inbox.last_cursor == 3


if __name__ == "__main__":
    pytest_bazel.main()
