"""Replay and conflict checks for the one-off cross-database import."""

import json
from uuid import UUID, uuid4

import pytest
import pytest_bazel
from google.protobuf.json_format import MessageToDict
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from agentplane.protocol import event_log_pb2, event_pb2
from agentplane.sandbox_service.session_history.backfill import backfill, import_log
from agentplane.sandbox_service.session_history.store import HistoryConflictError, Store

pytestmark = pytest.mark.asyncio


async def test_legacy_and_reserved_session_backfill_can_replay(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.execute(text("CREATE TABLE event_log(id uuid PRIMARY KEY, sandbox text, session_id text)"))
        await connection.execute(text("CREATE TABLE event(thread_id uuid, cursor bigint, payload json)"))
    store = Store(engine)
    legacy, managed = uuid4(), uuid4()
    uid = uuid4()
    await store.open(
        managed, sandbox_namespace="testing", sandbox_name="sb", sandbox_uid=uid, runner_session_id=f"r-{managed}"
    )

    async def add_log(session_id: UUID, runner_id: str) -> None:
        async with engine.begin() as connection:
            await connection.execute(
                text("INSERT INTO event_log (id, sandbox, session_id) VALUES (:id, 'sb', :runner_id)"),
                {"id": session_id, "runner_id": runner_id},
            )

    async def add_event(session_id: UUID, cursor: int, *, text_value: str = "text") -> None:
        entry = event_log_pb2.EventEntry(
            cursor=cursor,
            origin=event_log_pb2.EventOrigin(source_id="runner", sequence=cursor),
            event=event_pb2.Event(harness_stderr=event_pb2.HarnessStderr(text=text_value)),
        )
        async with engine.begin() as connection:
            await connection.execute(
                text("INSERT INTO event (thread_id, cursor, payload) VALUES (:id, :cursor, CAST(:payload AS json))"),
                {"id": session_id, "cursor": cursor, "payload": json.dumps(MessageToDict(entry))},
            )

    await add_log(legacy, f"s-{legacy}")
    await add_log(managed, str(managed))
    await add_event(legacy, 1)
    await add_event(managed, 1)
    assert await backfill(engine, engine, namespace="testing") == (2, 2)
    assert await backfill(engine, engine, namespace="testing") == (2, 2)
    assert (await store.read(legacy))[0] == 1
    assert (await store.read(managed))[0] == 1
    assert (
        await store.runner_id(managed, sandbox_namespace="testing", sandbox_name="sb", sandbox_uid=uid)
        == f"r-{managed}"
    )
    await add_event(legacy, 2)
    assert (
        await import_log(engine, engine, namespace="testing", log_id=legacy, sandbox="sb", runner_id=f"s-{legacy}") == 2
    )
    assert (await store.read(legacy))[0] == 2
    async with engine.begin() as connection:
        await connection.execute(
            text("UPDATE event SET payload = CAST(:payload AS json) WHERE thread_id = :id AND cursor = 1"),
            {"id": legacy, "payload": json.dumps({"cursor": "1"})},
        )
    with pytest.raises(HistoryConflictError):
        await backfill(engine, engine, namespace="testing")


if __name__ == "__main__":
    pytest_bazel.main()
