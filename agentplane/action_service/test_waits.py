"""Real commit notifications, bounded waits, races, and cross-writer receipt ownership."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
import pytest_bazel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from agentplane.action_service.catalog import ActionCatalog, ActionIdentity
from agentplane.action_service.db import ActionNotFoundError, ActionStore, make_sessionmaker
from agentplane.action_service.models import (
    ActionRequestInput,
    ActionRequestView,
    ActionState,
    CallerPrincipal,
    DecisionInput,
    ExecutionResult,
    ExecutionState,
    OperatorPrincipal,
    ReadPrincipal,
    Verdict,
)
from agentplane.action_service.service import ActionService
from agentplane.action_service.updates import CHANNEL, ActionUpdates, UpdatesUnavailableError
from agentplane.action_service.waits import ActionWaiter, WaitOptions, WaitUntil
from agentplane.subjects import ServiceAccountRef

CALLER = CallerPrincipal(account=ServiceAccountRef(namespace="agentplane-test", name="test-caller"))
OTHER = CallerPrincipal(account=ServiceAccountRef(namespace="agentplane-test", name="test-other-caller"))
OPERATOR = OperatorPrincipal(issuer="test", subject="operator")


class ObservedService(ActionService):
    """A read boundary signal lets tests order commits against actual waiter reads."""

    def __init__(self, store: ActionStore, catalog: ActionCatalog) -> None:
        super().__init__(store, catalog, {})
        self.reads: asyncio.Queue[ActionRequestView] = asyncio.Queue()

    async def get(self, request_id: UUID, principal: ReadPrincipal) -> ActionRequestView:
        view = await super().get(request_id, principal)
        self.reads.put_nowait(view)
        return view


@dataclass
class Waiting:
    writer: ActionStore
    service: ObservedService
    updates: ActionUpdates
    waiter: ActionWaiter
    request: ActionRequestView


@pytest.fixture
async def waiting(engine: AsyncEngine, db_url: str, echo_catalog: ActionCatalog) -> AsyncIterator[Waiting]:
    # The writer uses a separate session/store from the reader; only PostgreSQL connects them.
    writer = ActionStore(make_sessionmaker(engine))
    reader = ObservedService(ActionStore(make_sessionmaker(engine)), echo_catalog)
    request = await writer.submit(
        ActionRequestInput(
            action=ActionIdentity(group="agentplane", name="echo"),
            arguments={},
            idempotency_key="test-wait",
            title="test title for test-wait",
        ),
        CALLER,
        request_id=uuid4(),
        vote=None,
    )
    updates = ActionUpdates(db_url)
    async with updates.listener.listen():
        yield Waiting(writer, reader, updates, ActionWaiter(reader, updates, max_wait_seconds=30), request)


async def test_all_subscribers_receive_cross_replica_commit(waiting: Waiting, db_url: str) -> None:
    second = ActionUpdates(db_url)
    async with second.listener.connection():
        with waiting.updates.subscribe_all() as first, second.subscribe_all() as other:
            await decide(waiting, Verdict.DENY)
            async with asyncio.timeout(10):
                await asyncio.gather(first.changed.wait(), other.changed.wait())
            assert (await waiting.service.get(waiting.request.id, CALLER)).state is ActionState.DENIED
        assert not second._all_subscribers


async def test_all_subscribers_wake_on_channel_loss(waiting: Waiting) -> None:
    with waiting.updates.subscribe_all() as subscription:
        assert waiting.updates.listener._connection is not None
        waiting.updates.listener._connection.terminate()
        async with asyncio.timeout(10):
            await waiting.updates.listener.wait_until_disconnected()
        assert subscription.changed.is_set()
        with pytest.raises(UpdatesUnavailableError):
            subscription.check_available()


async def test_listener_recovers_but_old_subscriptions_stay_unavailable(
    waiting: Waiting, monkeypatch: pytest.MonkeyPatch
) -> None:
    restarted = asyncio.Event()
    connect = waiting.updates.listener._connect

    async def observed_connect() -> None:
        await connect()
        restarted.set()

    monkeypatch.setattr(waiting.updates.listener, "_connect", observed_connect)
    with (
        waiting.updates.subscribe_all() as all_updates,
        waiting.updates.subscribe(waiting.request.id) as request_updates,
    ):
        assert waiting.updates.listener._connection is not None
        waiting.updates.listener._connection.terminate()
        async with asyncio.timeout(10):
            await restarted.wait()
        assert waiting.updates.listener.connected
        for subscription in (all_updates, request_updates):
            with pytest.raises(UpdatesUnavailableError):
                subscription.check_available()
        with waiting.updates.subscribe_all() as fresh:
            fresh.check_available()
            await decide(waiting, Verdict.DENY)
            async with asyncio.timeout(10):
                await fresh.changed.wait()


async def subscribed(waiting: Waiting) -> None:
    # Authorization read, then the race-closing read after subscription.
    for _ in range(2):
        assert (await waiting.service.reads.get()).state is ActionState.DECISION_PENDING


async def decide(waiting: Waiting, verdict: Verdict) -> ActionRequestView:
    await waiting.writer.decide(
        waiting.request.id,
        DecisionInput(verdict=verdict, expected_version=1, idempotency_key="test-decision"),
        OPERATOR,
        provider="test-human",
    )
    return await waiting.writer.get(waiting.request.id, CALLER)


async def test_cross_writer_decision_and_terminal_predicates(waiting: Waiting) -> None:
    async with asyncio.timeout(10):
        terminal = asyncio.create_task(waiting.waiter.get(waiting.request.id, CALLER, WaitOptions(wait_seconds=10)))
        await subscribed(waiting)
        allowed = await decide(waiting, Verdict.ALLOW)
        assert (await waiting.service.reads.get()).state is ActionState.ALLOWED
        assert not terminal.done()
        assert (
            await waiting.waiter.get(
                waiting.request.id, CALLER, WaitOptions(wait_seconds=10, wait_until=WaitUntil.DECISION)
            )
            == allowed
        )
        claim = await waiting.writer.claim_execution(
            waiting.request.id, executor_id="test-executor", lease_duration=timedelta(seconds=30)
        )
        assert claim is not None
        await waiting.writer.mark_running(waiting.request.id)
        await waiting.writer.finish_execution(
            waiting.request.id,
            claim.executor_id,
            claim.lease_token,
            ExecutionResult(state=ExecutionState.SUCCEEDED, result={"token": "test-secret"}),
        )
        completed = await terminal
        assert completed.state is ActionState.SUCCEEDED
        assert completed.execution is not None
        assert completed.execution.result == {"token": "[redacted]"}
        assert not waiting.updates._subscribers


@pytest.mark.parametrize("until", list(WaitUntil))
async def test_denial_satisfies_both_predicates(waiting: Waiting, until: WaitUntil) -> None:
    async with asyncio.timeout(10):
        task = asyncio.create_task(
            waiting.waiter.get(waiting.request.id, CALLER, WaitOptions(wait_seconds=10, wait_until=until))
        )
        await subscribed(waiting)
        denied = await decide(waiting, Verdict.DENY)
        assert await task == denied


@pytest.mark.parametrize("until", list(WaitUntil))
async def test_cancellation_push_satisfies_both_predicates(waiting: Waiting, until: WaitUntil) -> None:
    async with asyncio.timeout(10):
        task = asyncio.create_task(
            waiting.waiter.get(waiting.request.id, CALLER, WaitOptions(wait_seconds=10, wait_until=until))
        )
        await subscribed(waiting)
        cancelled = await waiting.writer.cancel(waiting.request.id, CALLER)
        assert (await task).state is ActionState.CANCELLED
        assert await task == cancelled.request
        assert not waiting.updates._subscribers


async def test_cancelling_unclaimed_execution_wakes_terminal_wait(waiting: Waiting) -> None:
    async with asyncio.timeout(10):
        task = asyncio.create_task(waiting.waiter.get(waiting.request.id, CALLER, WaitOptions(wait_seconds=10)))
        await subscribed(waiting)
        await decide(waiting, Verdict.ALLOW)
        assert (await waiting.service.reads.get()).state is ActionState.ALLOWED
        assert not task.done()
        cancelled = await waiting.writer.cancel(waiting.request.id, CALLER)
        assert await task == cancelled.request
        assert cancelled.request.execution is not None
        assert cancelled.request.execution.state is ExecutionState.CANCELLED
        assert cancelled.request.execution.started_at is None


async def test_timeout_returns_current_receipt_and_cancel_only_cleans_wait(waiting: Waiting) -> None:
    receipt = await waiting.waiter.get(waiting.request.id, CALLER, WaitOptions(wait_seconds=0.001))
    assert receipt.state is ActionState.DECISION_PENDING
    assert not waiting.updates._subscribers
    while not waiting.service.reads.empty():
        waiting.service.reads.get_nowait()
    task = asyncio.create_task(waiting.waiter.get(waiting.request.id, CALLER, WaitOptions(wait_seconds=10)))
    await subscribed(waiting)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not waiting.updates._subscribers
    assert (await waiting.writer.get(waiting.request.id, CALLER)).state is ActionState.DECISION_PENDING


async def test_listener_loss_fails_wait_but_immediate_read_recovers(waiting: Waiting) -> None:
    async with asyncio.timeout(10):
        task = asyncio.create_task(waiting.waiter.get(waiting.request.id, CALLER, WaitOptions(wait_seconds=10)))
        await subscribed(waiting)
        assert waiting.updates.listener._connection is not None
        waiting.updates.listener._connection.terminate()
        with pytest.raises(UpdatesUnavailableError, match="wait_seconds=0"):
            await task
        assert not waiting.updates._subscribers
        assert (await waiting.waiter.get(waiting.request.id, CALLER, WaitOptions())).id == waiting.request.id


@pytest.mark.parametrize("held_read", [2, 3], ids=["subscribed-read", "timeout-read"])
async def test_wait_held_across_reconnection_fails_even_with_a_terminal_receipt(
    waiting: Waiting, db_url: str, monkeypatch: pytest.MonkeyPatch, held_read: int
) -> None:
    updates = ActionUpdates(db_url)
    waiter = ActionWaiter(waiting.service, updates, max_wait_seconds=30)
    reading, release, reconnected = asyncio.Event(), asyncio.Event(), asyncio.Event()
    get = waiting.service.get
    reads = 0

    async def held_get(request_id: UUID, principal: ReadPrincipal) -> ActionRequestView:
        nonlocal reads
        reads += 1
        if reads == held_read:
            reading.set()
            await release.wait()
        return await get(request_id, principal)

    invalidated = updates.listener._invalidated

    def observe_connection() -> None:
        invalidated()
        if updates.listener.connected:
            reconnected.set()

    monkeypatch.setattr(waiting.service, "get", held_get)
    monkeypatch.setattr(updates.listener, "_invalidated", observe_connection)
    async with asyncio.timeout(10), updates.listener.listen():
        reconnected.clear()
        task = asyncio.create_task(waiter.get(waiting.request.id, CALLER, WaitOptions(wait_seconds=0.001)))
        try:
            await reading.wait()
            assert updates.listener._connection is not None
            updates.listener._connection.terminate()
            await reconnected.wait()
            # The observer only resumes after the new connection is healthy. Neither a transient
            # availability flag nor a reconnect delay can preserve this wait's loss contract.
            await decide(waiting, Verdict.DENY)
            release.set()
            with pytest.raises(UpdatesUnavailableError, match="wait_seconds=0"):
                await task
            assert not updates._subscribers
            assert (await waiter.get(waiting.request.id, CALLER, WaitOptions())).state is ActionState.DENIED
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_other_caller_cannot_subscribe(waiting: Waiting) -> None:
    with pytest.raises(ActionNotFoundError):
        await waiting.waiter.get(waiting.request.id, OTHER, WaitOptions(wait_seconds=10))
    assert not waiting.updates._subscribers


async def test_rollback_and_duplicate_invalidations_keep_durable_state_authoritative(
    waiting: Waiting, engine: AsyncEngine
) -> None:
    async with asyncio.timeout(10):
        # A barrier notification on the same channel establishes delivery order without sleeps.
        barrier = uuid4()
        with (
            waiting.updates.subscribe(waiting.request.id) as request_updates,
            waiting.updates.subscribe(barrier) as delivered,
        ):
            async with engine.connect() as connection:
                transaction = await connection.begin()
                await connection.execute(select(func.pg_notify(CHANNEL, str(waiting.request.id))))
                await transaction.rollback()
                await connection.execute(select(func.pg_notify(CHANNEL, str(barrier))))
                await connection.commit()
            await delivered.changed.wait()
            assert not request_updates.changed.is_set()
        task = asyncio.create_task(waiting.waiter.get(waiting.request.id, CALLER, WaitOptions(wait_seconds=10)))
        await subscribed(waiting)
        async with engine.begin() as connection:
            for _ in range(2):
                await connection.execute(select(func.pg_notify(CHANNEL, str(waiting.request.id))))
        assert (await waiting.service.reads.get()).state is ActionState.DECISION_PENDING
        assert not task.done()
        await decide(waiting, Verdict.DENY)
        assert (await task).state is ActionState.DENIED


if __name__ == "__main__":
    pytest_bazel.main()
