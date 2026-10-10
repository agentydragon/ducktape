"""Bounded migration verification against isolated PostgreSQL, never deployed history."""

import json
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import pytest
import pytest_bazel
from google.protobuf.json_format import MessageToDict
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from agentplane.protocol import event_log_pb2, event_pb2
from agentplane.runner import protocol_pb2
from agentplane.sandbox_service.session_history.store import Store
from agentplane.sandbox_service.session_history.verify import Settings, VerificationError, read_primary, verify

# gazelle:include_dep @pypi//protobuf

pytestmark = pytest.mark.asyncio


async def seed(engine: AsyncEngine) -> Settings:
    session_id = uuid4()
    store = Store(engine, settle_deltas=False)
    await store.open(
        session_id, sandbox_namespace="testing", sandbox_name="sb", sandbox_uid=None, runner_session_id="s-old"
    )
    entries = [
        event_log_pb2.EventEntry(
            cursor=i,
            origin=event_log_pb2.EventOrigin(source_id="source", sequence=i),
            event=event_pb2.Event(harness_stderr=event_pb2.HarnessStderr(text=f"text-{i}")),
        )
        for i in range(1, 5)
    ]
    await store.append(session_id, entries)
    async with engine.begin() as connection:
        await connection.execute(text("CREATE TABLE event_log(id uuid PRIMARY KEY, sandbox text, session_id text)"))
        await connection.execute(
            text("CREATE TABLE event(thread_id uuid, cursor bigint, payload json, PRIMARY KEY(thread_id, cursor))")
        )
        await connection.execute(text("INSERT INTO event_log VALUES (:id, 'sb', 's-old')"), {"id": session_id})
        for entry in entries:
            await connection.execute(
                text("INSERT INTO event VALUES (:id, :cursor, CAST(:payload AS json))"),
                {"id": session_id, "cursor": entry.cursor, "payload": json.dumps(MessageToDict(entry))},
            )
    return Settings(
        _cli_parse_args=False,
        mode="archive",
        session_id=session_id,
        through=3,
        batch_size=1,
        max_batches=2,
        database_url="unused",
        app_database_url="unused",
    )


async def test_bounded_resume_and_growth_beyond_watermark(
    engine: AsyncEngine, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = await seed(engine)
    # Both databases already contain Event 4, beyond the fixed watermark 3.
    assert await verify(settings, engine, engine) == 2
    reports = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert reports[-1]["verified_through"] == 2
    settings.after = 2
    assert await verify(settings, engine, engine) == 3
    assert (await Store(engine, settle_deltas=False).read(settings.session_id))[0] == 4


async def test_tail_sample_does_not_read_unselected_prefix(engine: AsyncEngine) -> None:
    settings = await seed(engine)
    # A tail check is deliberately not a full-prefix proof. Missing earlier source
    # rows must neither trigger a scan nor make the selected range fail.
    async with engine.begin() as connection:
        await connection.execute(text("DELETE FROM event WHERE cursor <= 2"))
    settings.after = 2
    settings.max_batches = 1
    assert await verify(settings, engine, engine) == 3


@pytest.mark.parametrize("change", ["payload", "gap", "source", "origin_sequence", "checkpoint", "locator"])
async def test_conflicting_or_missing_evidence_fails(engine: AsyncEngine, change: str) -> None:
    settings = await seed(engine)
    async with engine.begin() as connection:
        if change == "gap":
            await connection.execute(text("DELETE FROM event WHERE cursor=2"))
        elif change == "checkpoint":
            settings.through = 5
        elif change == "locator":
            await connection.execute(text("UPDATE event_log SET session_id='different'"))
        else:
            entry = event_log_pb2.EventEntry(
                cursor=2,
                origin=event_log_pb2.EventOrigin(
                    source_id="other" if change == "source" else "source",
                    sequence=9 if change == "origin_sequence" else 2,
                ),
                event=event_pb2.Event(harness_stderr=event_pb2.HarnessStderr(text="changed")),
            )
            await connection.execute(
                text("UPDATE event SET payload=CAST(:payload AS json) WHERE cursor=2"),
                {"payload": json.dumps(MessageToDict(entry))},
            )
    with pytest.raises(VerificationError):
        await verify(settings, engine, engine)


async def test_verifier_transaction_rejects_writes(engine: AsyncEngine) -> None:
    with pytest.raises(DBAPIError):
        async with read_primary(engine) as connection:
            await connection.execute(text("DELETE FROM session_history"))


@pytest.mark.parametrize("case", ["match", "missing", "conflict", "behind"])
async def test_runner_overlap_uses_only_existing_session_and_never_writes_binding(
    engine: AsyncEngine, case: str
) -> None:
    settings = await seed(engine)
    settings.mode = "runner"
    settings.runner_target = "unused:7000"
    settings.max_batches = 4
    _, entries = await Store(engine, settle_deltas=False).read(settings.session_id)
    attachment = Mock()
    attachment.attached = protocol_pb2.Attached(last_cursor=2 if case == "behind" else 4)
    if case == "conflict":
        entries[1].origin.source_id = "different-source"
    attachment.next_entry = AsyncMock(side_effect=entries)
    runner = Mock()
    runner.list_sessions = AsyncMock(
        return_value=[protocol_pb2.SessionSummary(session_id="s-old")] if case != "missing" else []
    )
    runner.attach = AsyncMock(return_value=attachment)
    runner.close = AsyncMock()
    with (
        patch("agentplane.sandbox_service.session_history.verify.RunnerClient", return_value=runner),
        patch("agentplane.sandbox_service.session_history.verify.grpc.aio.insecure_channel", return_value=Mock()),
    ):
        if case == "match":
            assert await verify(settings, engine, None) == 3
            runner.attach.assert_awaited_once_with("s-old", after_cursor=0)
            attachment.cancel.assert_called_once()
        else:
            with pytest.raises(VerificationError):
                await verify(settings, engine, None)
            if case == "missing":
                runner.attach.assert_not_awaited()
            else:
                attachment.cancel.assert_called_once()
    runner.close.assert_awaited_once()
    async with engine.connect() as connection:
        assert (
            await connection.scalar(
                text("SELECT sandbox_uid FROM session_history WHERE id=:id"), {"id": settings.session_id}
            )
            is None
        )


if __name__ == "__main__":
    pytest_bazel.main()
