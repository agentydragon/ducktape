"""Electric resumes only through an explicit client reset after a retained-WAL outage."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from datetime import timedelta
from pathlib import Path
from uuid import UUID

import asyncpg
import httpx
import pytest_bazel

from agentplane.app.testing.electric_service import ElectricService, electric_service
from agentplane.app.testing.replication_source import SANDBOX, SESSION, ReplicationSource
from agentplane.app.trajectory import IngestionLease, TrajectoryStore
from agentplane.protocol import event_pb2
from util.testing.undeclared_outputs import undeclared_outputs_dir

# gazelle:include_dep @pypi//protobuf

_SLOT = "electric_slot_agentplane_conversation"
_WAL_CAP_BYTES = 1024 * 1024
_BATCH_BYTES = 4 * 1024 * 1024


async def test_electric_lagging_slot_forces_client_resnapshot_after_wal_cap() -> None:
    """A stopped Electric service loses its slot and cannot continue an old shape cursor."""
    with tempfile.TemporaryDirectory(prefix="electric-wal-state-") as state_dir:
        async with electric_service(
            postgres_settings=("max_slot_wal_keep_size=1MB", "max_wal_size=32MB", "min_wal_size=32MB"),
            electric_storage_dir=Path(state_dir),
        ) as service:
            store = TrajectoryStore.connect(service.database_url)
            try:
                thread, source, lease = await _project_initial_item(store)
                params = {"table": "conversation_entity", "where": f"thread_id = '{thread}'"}
                async with httpx.AsyncClient(base_url=service.url, timeout=35) as client:
                    initial = await client.get("/v1/shape", params=params | {"offset": "-1"})
                    initial.raise_for_status()
                    assert {row["entity_id"] for row in _values(initial)} >= {"first"}
                    old_offset = initial.headers["electric-offset"]
                    old_handle = initial.headers["electric-handle"]

                    connection = await _connect(service)
                    timeline: list[dict[str, object]] = []
                    try:
                        timeline.append({"phase": "healthy", "slot": await _slot_state(connection)})
                        await service.stop()
                        await _wait_for_inactive_slot(connection)
                        timeline.append({"phase": "stopped", "slot": await _slot_state(connection)})
                        await connection.execute("CREATE TABLE electric_wal_noise (payload bytea NOT NULL)")
                        start_lsn = await connection.fetchval("SELECT pg_current_wal_lsn()")
                        for batch in range(20):
                            await connection.executemany(
                                "INSERT INTO electric_wal_noise (payload) VALUES ($1)",
                                [(os.urandom(_BATCH_BYTES // 8),) for _ in range(8)],
                            )
                            await connection.execute("CHECKPOINT")
                            state = await _slot_state(connection)
                            written = int(
                                await connection.fetchval("SELECT pg_wal_lsn_diff(pg_current_wal_lsn(), $1)", start_lsn)
                            )
                            timeline.append(
                                {"phase": "outage_write", "batch": batch, "wal_bytes": written, "slot": state}
                            )
                            if state["wal_status"] == "lost":
                                break
                        _write_artifact("slot-timeline.json", json.dumps(timeline, indent=2, sort_keys=True))
                        latest = timeline[-1]
                        written = latest["wal_bytes"]
                        slot = latest["slot"]
                        assert isinstance(written, int)
                        assert isinstance(slot, dict)
                        assert written >= _WAL_CAP_BYTES
                        assert slot["wal_status"] == "lost"

                        # This commit must appear only after the client discards its old stream cursor.
                        source.append(
                            event_pb2.Event(
                                item_started=event_pb2.ItemStarted(
                                    item_id="during-outage", kind=event_pb2.ITEM_KIND_ASSISTANT_TEXT
                                )
                            )
                        )
                        source.append(
                            event_pb2.Event(text_delta=event_pb2.TextDelta(item_id="during-outage", text="fresh"))
                        )
                        await store.record(thread, source.entries[-2:], lease=lease)
                    finally:
                        await connection.close()

                    await service.start()
                    stale = await client.get(
                        "/v1/shape", params=params | {"offset": old_offset, "handle": old_handle, "live": "true"}
                    )
                    _write_artifact(
                        "stale-shape-response.json",
                        json.dumps(
                            {"status": stale.status_code, "headers": dict(stale.headers), "body": stale.text},
                            indent=2,
                            sort_keys=True,
                        ),
                    )
                    assert stale.status_code == 409, stale.text
                    resnapshot = await client.get("/v1/shape", params=params | {"offset": "-1"})
                    resnapshot.raise_for_status()
                    assert {row["entity_id"] for row in _values(resnapshot)} >= {"first", "during-outage"}
            finally:
                await store.close()


async def _project_initial_item(store: TrajectoryStore) -> tuple[UUID, ReplicationSource, IngestionLease]:
    source = ReplicationSource()
    source.append(event_pb2.Event(harness_started=event_pb2.HarnessStarted(pid=123)))
    source.append(event_pb2.Event(turn_started=event_pb2.TurnStarted(turn_id="turn", model="test-model")))
    source.append(
        event_pb2.Event(item_started=event_pb2.ItemStarted(item_id="first", kind=event_pb2.ITEM_KIND_ASSISTANT_TEXT))
    )
    source.append(event_pb2.Event(text_delta=event_pb2.TextDelta(item_id="first", text="before outage")))
    thread = await store.thread(SANDBOX, SESSION, source.attached.spec)
    lease = await store.acquire_ingestion(SANDBOX, timedelta(minutes=2))
    assert lease is not None
    await store.set_attached(thread, source.attached, lease=lease)
    await store.record(thread, source.entries, lease=lease)
    return thread, source, lease


async def _connect(service: ElectricService) -> asyncpg.Connection:
    return await asyncpg.connect(service.database_url.replace("postgresql+asyncpg", "postgresql"))


async def _slot_state(connection: asyncpg.Connection) -> dict[str, object]:
    row = await connection.fetchrow(
        """
        SELECT slot_name, active, restart_lsn::text, confirmed_flush_lsn::text, wal_status, safe_wal_size
        FROM pg_replication_slots
        WHERE slot_name = $1
        """,
        _SLOT,
    )
    assert row is not None
    return dict(row)


async def _wait_for_inactive_slot(connection: asyncpg.Connection) -> None:
    async with asyncio.timeout(30):
        while (await _slot_state(connection))["active"]:
            await connection.execute("SELECT pg_sleep(0.1)")


def _values(response: httpx.Response) -> list[dict[str, object]]:
    return [message["value"] for message in response.json() if "value" in message]


def _write_artifact(name: str, body: str) -> None:
    path = undeclared_outputs_dir() / name
    path.write_text(body + "\n")


if __name__ == "__main__":
    pytest_bazel.main()
