"""Shared history across writers/replacement, schema lifecycle, and explicit database failures."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import aiohttp
import pytest
import pytest_bazel
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import func, select
from sqlalchemy.engine import Connection

from x.agentplane.egress.admin import create_admin_app, serve_admin
from x.agentplane.egress.database_migrate import run_migrations_for_connection
from x.agentplane.egress.decision_log import DecisionLog
from x.agentplane.egress.decision_store import Base, DecisionRecordRow, DecisionStore, make_engine
from x.agentplane.egress.decisions import DecisionRecord, Outcome, Phase
from x.agentplane.egress.policy import WATCHED_KINDS, Index
from x.agentplane.subjects import SubjectKind, SubjectView

SUBJECT = SubjectView(kind=SubjectKind.SANDBOX, name="test-sandbox")


def record(**values) -> DecisionRecord:
    return DecisionRecord.model_validate(
        {
            "producer_id": uuid4(),
            "at": datetime.now(UTC),
            "subject": SUBJECT,
            "subject_namespace": "test-namespace",
            "sandbox_uid": "test-sandbox-uid",
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
    run_migrations_for_connection(connection)
    assert (
        compare_metadata(
            MigrationContext.configure(connection, opts={"version_table": "egress_alembic_version"}), Base.metadata
        )
        == []
    )


async def test_shared_history_survives_replacement_and_repeated_migration(history_db_url: str) -> None:
    first = DecisionLog(DecisionStore(make_engine(history_db_url), retention=timedelta(days=7)))
    second = DecisionLog(DecisionStore(make_engine(history_db_url), retention=timedelta(days=7)))
    a, b = record(), record()
    first.start()
    second.start()
    first.record(a)
    second.record(b)
    await asyncio.gather(first.flush(), second.flush())
    await asyncio.gather(first.close(), second.close())
    replacement = DecisionLog(DecisionStore(make_engine(history_db_url), retention=timedelta(days=7)))
    try:
        async with replacement.store.engine.begin() as connection:
            await connection.run_sync(check_migration)
        app = create_admin_app(replacement, Index(), resync_seconds=300)
        async with (
            serve_admin(app, "127.0.0.1", 0) as port,
            aiohttp.ClientSession(f"http://127.0.0.1:{port}") as client,
            client.get("/decisions", params={"kind": SUBJECT.kind, "name": SUBJECT.name}) as response,
        ):
            assert response.status == 200
            rows = [DecisionRecord.model_validate(item) for item in await response.json()]
        assert rows == [a, b]
        assert a.producer_id != b.producer_id
        assert await replacement.store.recent(SubjectView(kind=SubjectKind.SANDBOX, name="absent")) == []
        assert await replacement.store.recent(SUBJECT.model_copy(update={"kind": SubjectKind.SERVICE_ACCOUNT})) == [], (
            "the same name under the other kind is a different subject"
        )
    finally:
        await replacement.close()


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
    log.start()
    log.record(event)
    try:
        await log.flush()
        assert await log.store.recent(event.subject) == [event]
        assert calls == 2
        assert log.diagnostics.write_failures == 1
        assert log.diagnostics.acknowledged == 1
    finally:
        await log.close()


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


async def test_overflow_outage_nonblocking_and_explicit_read_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    log = DecisionLog(
        DecisionStore(make_engine("postgresql://test:test@127.0.0.1:1/test"), retention=timedelta(days=7)), queue_size=1
    )
    entered, release = asyncio.Event(), asyncio.Event()

    async def unavailable(records: list[DecisionRecord]) -> None:
        entered.set()
        await release.wait()
        raise OSError("test private exception must not escape")

    monkeypatch.setattr(log.store, "append", unavailable)
    log.record(record())
    log.record(record())
    assert log.diagnostics.dropped_overflow == 1
    log.start()
    await entered.wait()
    # The DB is blocked, but synchronous admission recording still completes.
    log.record(record())
    assert log.diagnostics.accepted == 2
    app = create_admin_app(
        log, Index(synced=True, refreshed=dict.fromkeys(WATCHED_KINDS, datetime.now(UTC))), resync_seconds=300
    )
    async with serve_admin(app, "127.0.0.1", 0) as port, aiohttp.ClientSession(f"http://127.0.0.1:{port}") as client:
        async with client.get("/decisions") as response:
            assert response.status == 503
            assert await response.json() == {"error": "decision-history-unavailable"}
        async with client.get("/healthz") as response:
            assert response.status == 200
            assert (await response.json())["decisionHistory"]["dropped_overflow"] == 1
    # Shutdown must not wait for the deliberately unreleased DB operation.
    await log.close(flush_seconds=0.01)
    assert not release.is_set()
    assert log.diagnostics.dropped_shutdown == 2


async def test_retry_budget_explicit_loss(monkeypatch: pytest.MonkeyPatch) -> None:
    log = DecisionLog(
        DecisionStore(make_engine("postgresql://test:test@127.0.0.1:1/test"), retention=timedelta(days=7))
    )

    async def unavailable(records: list[DecisionRecord]) -> None:
        raise OSError("test unavailable")

    monkeypatch.setattr(log.store, "append", unavailable)
    log.start()
    log.record(record())
    await log.flush()
    assert log.diagnostics.dropped_unavailable == 1
    assert log.diagnostics.write_failures == 3
    assert not log.diagnostics.available
    await log.close()


if __name__ == "__main__":
    pytest_bazel.main()
