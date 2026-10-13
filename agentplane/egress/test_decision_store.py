"""Shared history across writers/replacement, schema lifecycle, and explicit database failures."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
import pytest_bazel
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.engine import Connection, make_url

from agentplane.egress.admin import create_admin_app
from agentplane.egress.database_migrate import RUNNER
from agentplane.egress.decision_changes import DecisionChanges
from agentplane.egress.decision_log import DecisionLog
from agentplane.egress.decision_store import Base, DecisionRecordRow, DecisionStore, make_engine
from agentplane.egress.decisions import DecisionRecord, Outcome, Phase
from agentplane.egress.policy import WATCHED_KINDS, Index
from agentplane.kubernetes_watch import Freshness
from agentplane.subjects import ServiceAccountRef

SUBJECT = ServiceAccountRef(namespace="test-namespace", name="test-workload")


def admin_client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://egress-admin.test")


def record(**values) -> DecisionRecord:
    return DecisionRecord.model_validate(
        {
            "producer_id": uuid4(),
            "at": datetime.now(UTC),
            "subject": SUBJECT,
            "source_pod_uid": "test-pod-uid",
            "connection_id": "test-connection",
            "phase": Phase.HTTP_REQUEST,
            "method": "GET",
            "host": "example.test",
            "port": 443,
            "outcome": Outcome.ALLOW,
            **values,
        }
    )


def check_migration(connection: Connection) -> None:
    RUNNER.run_for_connection(connection)
    assert (
        compare_metadata(
            MigrationContext.configure(connection, opts={"version_table": "egress_alembic_version"}), Base.metadata
        )
        == []
    )


async def test_shared_history_survives_replacement_and_repeated_migration(history_db_url: str) -> None:
    a, b = record(), record()
    async with (
        DecisionLog(DecisionStore(make_engine(history_db_url), retention=timedelta(days=7))) as first,
        DecisionLog(DecisionStore(make_engine(history_db_url), retention=timedelta(days=7))) as second,
    ):
        first.record(a)
        second.record(b)
        await asyncio.gather(first.flush(), second.flush())
    async with DecisionLog(DecisionStore(make_engine(history_db_url), retention=timedelta(days=7))) as replacement:
        async with replacement.store.engine.begin() as connection:
            await connection.run_sync(check_migration)
        app = create_admin_app(replacement, Index(freshness=Freshness(stale_after_seconds=900)))
        async with admin_client(app) as client:
            response = await client.get("/decisions", params=SUBJECT.model_dump())
        assert response.status_code == 200
        rows = [DecisionRecord.model_validate(item) for item in response.json()]
        assert rows == [a, b]
        assert await replacement.store.recent(SUBJECT.model_copy(update={"name": "absent"})) == []
        assert await replacement.store.recent(SUBJECT.model_copy(update={"namespace": "elsewhere"})) == [], (
            "the same name in another namespace is a different subject"
        )


async def test_duplicate_acknowledgement_ambiguity(history_db_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    log = DecisionLog(DecisionStore(make_engine(history_db_url), retention=timedelta(days=7)))
    append = log.store.append
    calls = 0

    async def commit_then_disconnect(records: list[DecisionRecord]) -> None:
        nonlocal calls
        await append(records)
        calls += 1
        if calls == 1:
            raise OSError("test acknowledgement lost")

    monkeypatch.setattr(log.store, "append", commit_then_disconnect)
    event = record()
    async with log:
        log.record(event)
        await log.flush()
        assert await log.store.recent(event.subject) == [event]
        assert calls == 2
        assert log.diagnostics.write_failures == 1
        assert log.diagnostics.acknowledged == 1


async def test_retention_and_recent_limit(history_db_url: str) -> None:
    store = DecisionStore(make_engine(history_db_url), retention=timedelta(days=7), capacity=2)
    expired = record(at=datetime.now(UTC) - timedelta(days=8))
    current = [record() for _ in range(3)]
    try:
        await store.append([expired, *current])
        assert await store.recent(SUBJECT) == current[-2:]
        await store.cleanup()
        async with store.engine.connect() as connection:
            assert await connection.scalar(select(func.count()).select_from(DecisionRecordRow)) == 3
    finally:
        await store.engine.dispose()


async def test_changes_follow_wakes_when_a_replica_commits_decisions(history_db_url: str) -> None:
    # A heartbeat far past the timeout: only the commit can be what wakes the second step.
    stream = DecisionChanges(make_url(history_db_url), heartbeat_s=600).follow()
    async with DecisionLog(DecisionStore(make_engine(history_db_url), retention=timedelta(days=7))) as log:
        try:
            async with asyncio.timeout(10):
                await anext(stream)
                log.record(record())
                await anext(stream)
        finally:
            await stream.aclose()


async def test_overflow_outage_nonblocking_and_explicit_read_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    log = DecisionLog(
        DecisionStore(make_engine("postgresql://test:test@127.0.0.1:1/test"), retention=timedelta(days=7)),
        queue_size=1,
        flush_seconds=0.01,
    )
    entered, release = asyncio.Event(), asyncio.Event()

    async def unavailable(records: list[DecisionRecord]) -> None:
        entered.set()
        await release.wait()
        raise OSError("test private exception must not escape")

    monkeypatch.setattr(log.store, "append", unavailable)
    # Before entering, so no writer drains the queue yet.
    log.record(record())
    log.record(record())
    assert log.diagnostics.dropped_overflow == 1
    async with log:
        await entered.wait()
        # The DB is blocked, but synchronous admission recording still completes.
        log.record(record())
        assert log.diagnostics.accepted == 2
        app = create_admin_app(
            log,
            Index(
                freshness=Freshness(stale_after_seconds=900, at=dict.fromkeys(WATCHED_KINDS, datetime.now(UTC))),
                synced=True,
            ),
        )
        async with admin_client(app) as client:
            refused = await client.get("/decisions")
            health = await client.get("/healthz")
        assert refused.status_code == 503
        assert "test private exception" not in refused.text
        assert health.status_code == 200
        assert health.json()["decisionHistory"]["dropped_overflow"] == 1
    # Shutdown must not wait for the deliberately unreleased DB operation.
    assert not release.is_set()
    assert log.diagnostics.dropped_shutdown == 2


async def test_retry_budget_explicit_loss(monkeypatch: pytest.MonkeyPatch) -> None:
    log = DecisionLog(
        DecisionStore(make_engine("postgresql://test:test@127.0.0.1:1/test"), retention=timedelta(days=7))
    )

    async def unavailable(records: list[DecisionRecord]) -> None:
        raise OSError("test unavailable")

    monkeypatch.setattr(log.store, "append", unavailable)
    async with log:
        log.record(record())
        await log.flush()
        assert log.diagnostics.dropped_unavailable == 1
        assert log.diagnostics.write_failures == 3
        assert not log.diagnostics.available


if __name__ == "__main__":
    pytest_bazel.main()
