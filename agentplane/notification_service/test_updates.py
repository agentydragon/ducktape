"""Real PostgreSQL commit wakeups, replica fanout, recovery and durable retry deadlines."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest_bazel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from agentplane.notification_service.db import Inbox
from agentplane.notification_service.models import ActionsSource, DestinationRef, Subscribe
from agentplane.notification_service.store import Store
from agentplane.notification_service.updates import Wakeups, notify
from agentplane.workload_auth.principal import WorkloadPrincipal

PRINCIPAL = WorkloadPrincipal("test", "owner", "system:serviceaccount:test:owner", "pod", "pod-uid", "account-uid")
BODY = Subscribe(
    destination_ref=DestinationRef(namespace="test", name="sandbox", uid="sandbox-uid"),
    session_id="session",
    idempotency_key="wakeups",
    source=ActionsSource(provider="actions", request_id=uuid4()),
)


async def test_notify_is_commit_scoped_and_fans_out(engine: AsyncEngine, store: Store) -> None:
    first, second = Wakeups(engine.url), Wakeups(engine.url)
    async with first.listener.listen(), second.listener.listen():
        with first.subscribe() as a, second.subscribe() as b:
            async with store.sessions.begin() as session:
                await notify(session)
                assert not a.is_set()
                assert not b.is_set()
                await session.rollback()
            assert not a.is_set()
            assert not b.is_set()
            async with store.sessions.begin() as session:
                await notify(session)
                assert not a.is_set()
                assert not b.is_set()
            async with asyncio.timeout(10):
                await a.wait()
                await b.wait()


async def test_startup_and_reconnect_drain_durable_work(store: Store) -> None:
    # Acceptance before LISTEN cannot be recovered from NOTIFY; it is recovered from the row.
    sub = await store.subscribe(PRINCIPAL, BODY)
    with store.wakeups.subscribe() as changed:
        async with store.wakeups.listener.listen():
            assert changed.is_set()
            claim = await store.claim()
            assert claim is not None
            assert claim.id == sub.inbox_id
            # A worker dying with a claim leaves a lease deadline, not a lost in-memory job.
            next_work_at = await store.get_next_work_at()
            assert next_work_at == claim.claim_until
            assert datetime.now(UTC) < next_work_at <= datetime.now(UTC) + timedelta(seconds=30)
        async with store.sessions.begin() as session:
            inbox = await session.get(Inbox, claim.id)
            assert inbox is not None
            inbox.claim_until = datetime.now(UTC) - timedelta(seconds=1)
            await notify(session)
        changed.clear()
        async with store.wakeups.listener.listen():
            assert changed.is_set()
            recovered = await store.claim()
            assert recovered is not None
            assert recovered.claim != claim.claim


async def test_cancelled_inbox_has_no_periodic_work(store: Store) -> None:
    sub = await store.subscribe(PRINCIPAL, BODY)
    claim = await store.claim()
    assert claim is not None
    await store.change(PRINCIPAL.account, sub.id, None)
    await store.release(claim, None)
    assert await store.get_next_work_at() is None
    assert await store.claim() is None
    async with store.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Inbox)) == 1


if __name__ == "__main__":
    pytest_bazel.main()
