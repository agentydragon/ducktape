"""One Electric shape per thread serving a moving window, pinned against the Electric we deploy.

A library spike: Electric documents each piece — `log=changes_only`, subset snapshots, the metadata
that lets a client skip a change a snapshot already holds, array parameters — but not that they
compose into a window that moves inside one shape whose predicate never changes. These tests pin
that they do, and the request forms that work.
"""

import asyncio
from collections.abc import Callable
from datetime import timedelta
from typing import Any
from uuid import UUID

import httpx
import pytest_bazel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from agentplane.app.agent_runtime.events.event_log import EventLogStore
from agentplane.app.agent_runtime.events.ingestion_lease import IngestionLease
from agentplane.app.agent_runtime.ingestion import Ingestion
from agentplane.app.agent_runtime.models import ThreadEntity
from agentplane.app.agent_runtime.view.recording import THREAD_FOLD_EPOCH
from agentplane.app.database import connect
from agentplane.app.testing.electric_service import electric_service
from agentplane.app.testing.replication_source import SANDBOX, SESSION, ReplicationSource
from agentplane.protocol import event_pb2

# gazelle:include_dep @pypi//protobuf

Row = dict[str, Any]
_SHAPE = "/v1/shape"


def _thread_shape(table: str, thread: UUID, **extra: str) -> dict[str, str]:
    """A shape over one thread's rows. Nothing about a window is in it, so it never changes."""
    where = " AND ".join(
        ["thread_id = $1", "projection_epoch = $2", *(f"{column} = ${index}" for index, column in enumerate(extra, 3))]
    )
    params = {"params[1]": str(thread), "params[2]": THREAD_FOLD_EPOCH}
    params.update({f"params[{index}]": value for index, value in enumerate(extra.values(), 3)})
    return {"table": table, "where": where, "log": "changes_only", "replica": "full", **params}


async def _open(client: httpx.AsyncClient, shape: dict[str, str]) -> tuple[str, str]:
    """Create the shape from now: no rows, only where its live log starts."""
    response = await client.get(_SHAPE, params=shape | {"offset": "now"})
    response.raise_for_status()
    return response.headers["electric-handle"], response.headers["electric-offset"]


async def _subset(
    client: httpx.AsyncClient, shape: dict[str, str], handle: str, offset: str, body: dict[str, object]
) -> tuple[list[Row], dict[str, Any]]:
    response = await client.post(_SHAPE, params=shape | {"handle": handle, "offset": offset}, json=body)
    assert response.status_code == 200, response.text
    snapshot = response.json()
    return [message["value"] for message in snapshot["data"] if "value" in message], snapshot["metadata"]


async def _follow_until(
    client: httpx.AsyncClient,
    shape: dict[str, str],
    handle: str,
    offset: str,
    done: Callable[[list[dict[str, Any]]], bool],
) -> list[dict[str, Any]]:
    """Every change on the shape's live log from `offset`, until `done` says enough arrived."""
    changes: list[dict[str, Any]] = []
    while not done(changes):
        response = await client.get(_SHAPE, params=shape | {"handle": handle, "offset": offset, "live": "true"})
        response.raise_for_status()
        offset = response.headers["electric-offset"]
        changes.extend(message for message in response.json() if "value" in message)
    return changes


def _visible(txid: int, metadata: dict[str, Any]) -> bool:
    """Whether a snapshot already holds a transaction's effects: Postgres snapshot visibility over the
    `xmin`/`xmax`/`xip_list` a subset reports, which is how a client skips a live change it has."""
    if txid < int(metadata["xmin"]):
        return True
    return txid < int(metadata["xmax"]) and txid not in {int(xid) for xid in metadata["xip_list"]}


async def _rows(engine: AsyncEngine, thread: UUID) -> list[ThreadEntity]:
    async with async_sessionmaker(engine)() as session:
        return list(await session.scalars(select(ThreadEntity).where(ThreadEntity.thread_id == thread)))


def _key(row: Row) -> tuple[str, str]:
    return row["entity_kind"], row["entity_id"]


async def _record(
    ingestion: Ingestion, source: ReplicationSource, thread: UUID, lease: IngestionLease, *events: event_pb2.Event
) -> None:
    await ingestion.record(thread, [source.append(event) for event in events], lease=lease)


async def test_one_thread_shape_serves_a_moving_window() -> None:
    async with electric_service() as service:
        engine = connect(service.database_url)
        try:
            event_logs, ingestion = EventLogStore(engine), Ingestion(engine)
            source = ReplicationSource()
            thread = await event_logs.open(SANDBOX, SESSION, source.attached.spec)
            lease = await ingestion.acquire(SANDBOX, timedelta(minutes=2))
            assert lease is not None
            await ingestion.set_attached(thread, source.attached, lease=lease)
            items = [f"item-{index:02}" for index in range(12)]
            await _record(
                ingestion,
                source,
                thread,
                lease,
                event_pb2.Event(harness_started=event_pb2.HarnessStarted(pid=1)),
                event_pb2.Event(turn_started=event_pb2.TurnStarted(turn_id="turn", model="test-model")),
                *(
                    event
                    for index, item in enumerate(items)
                    for event in (
                        event_pb2.Event(
                            item_started=event_pb2.ItemStarted(item_id=item, kind=event_pb2.ITEM_KIND_ASSISTANT_TEXT)
                        ),
                        event_pb2.Event(text_delta=event_pb2.TextDelta(item_id=item, text=f"Body {index}")),
                    )
                ),
            )
            indexes = sorted((row.entity_index for row in await _rows(engine, thread)), reverse=True)
            entities = _thread_shape("thread_entity", thread)
            async with asyncio.timeout(120), httpx.AsyncClient(base_url=service.url, timeout=35) as client:
                handle, start = await _open(client, entities)

                # Opening on the tail: the newest positions, however many kinds occupy them.
                tail, _ = await _subset(client, entities, handle, start, {"order_by": "entity_index DESC", "limit": 6})
                assert sorted((int(row["entity_index"]) for row in tail), reverse=True) == indexes[:6]

                # An edit to a row the reader does not hold yet, committed before it scrolls there.
                await _record(
                    ingestion,
                    source,
                    thread,
                    lease,
                    event_pb2.Event(text_delta=event_pb2.TextDelta(item_id="item-01", text=" edited")),
                )
                before_scroll = next(row for row in await _rows(engine, thread) if row.entity_id == "item-01")

                # Scrolling up is a second subset of the same shape: only positions not yet held.
                older, older_snapshot = await _subset(
                    client,
                    entities,
                    handle,
                    start,
                    {
                        "where": "entity_index < $1",
                        "params": {"1": str(min(int(row["entity_index"]) for row in tail))},
                        "order_by": "entity_index DESC",
                        "limit": 100,
                    },
                )
                assert sorted((int(row["entity_index"]) for row in older), reverse=True) == indexes[6:]
                assert not {_key(row) for row in older} & {_key(row) for row in tail}
                item_01 = next(row for row in older if row["entity_id"] == "item-01")
                assert int(item_01["revision_cursor"]) == before_scroll.revision_cursor

                # An edit to a row the reader loaded by scrolling up, committed after it did.
                await _record(
                    ingestion,
                    source,
                    thread,
                    lease,
                    event_pb2.Event(text_delta=event_pb2.TextDelta(item_id="item-01", text=" again")),
                )
                after_scroll = next(row for row in await _rows(engine, thread) if row.entity_id == "item-01")
                assert after_scroll.revision_cursor > before_scroll.revision_cursor

                # Both edits are on the one live log. The snapshot's metadata says the first is
                # already in what the scroll loaded, so a client skips it and applies the second.
                changes = await _follow_until(
                    client,
                    entities,
                    handle,
                    start,
                    lambda seen: any(
                        change["value"]["entity_id"] == "item-01"
                        and int(change["value"]["revision_cursor"]) == after_scroll.revision_cursor
                        for change in seen
                    ),
                )
                edits = {
                    int(change["value"]["revision_cursor"]): [int(txid) for txid in change["headers"]["txids"]]
                    for change in changes
                    if change["value"]["entity_id"] == "item-01"
                }
                assert set(edits) == {before_scroll.revision_cursor, after_scroll.revision_cursor}
                assert all(_visible(txid, older_snapshot) for txid in edits[before_scroll.revision_cursor])
                assert not any(_visible(txid, older_snapshot) for txid in edits[after_scroll.revision_cursor])
        finally:
            await engine.dispose()


async def test_bodies_load_as_subsets_of_one_shape_per_field() -> None:
    async with electric_service() as service:
        engine = connect(service.database_url)
        try:
            event_logs, ingestion = EventLogStore(engine), Ingestion(engine)
            source = ReplicationSource()
            thread = await event_logs.open(SANDBOX, SESSION, source.attached.spec)
            lease = await ingestion.acquire(SANDBOX, timedelta(minutes=2))
            assert lease is not None
            await ingestion.set_attached(thread, source.attached, lease=lease)
            await _record(
                ingestion,
                source,
                thread,
                lease,
                event_pb2.Event(harness_started=event_pb2.HarnessStarted(pid=1)),
                event_pb2.Event(turn_started=event_pb2.TurnStarted(turn_id="turn", model="test-model")),
                *(
                    event
                    for item in ("replaced", "kept", "unrelated")
                    for event in (
                        event_pb2.Event(
                            item_started=event_pb2.ItemStarted(item_id=item, kind=event_pb2.ITEM_KIND_ASSISTANT_TEXT)
                        ),
                        event_pb2.Event(text_delta=event_pb2.TextDelta(item_id=item, text=f"First {item}")),
                    )
                ),
            )

            def reference(rows: list[ThreadEntity], item: str) -> dict[str, Any]:
                text_ref = next(row for row in rows if row.entity_id == item).text_ref
                assert text_ref is not None
                return text_ref

            first_generation = reference(await _rows(engine, thread), "replaced")["generation"]
            # A completion whose text differs from what streamed replaces the body: a new
            # generation, with the old one's chunks still in the insert-only table.
            await _record(
                ingestion,
                source,
                thread,
                lease,
                event_pb2.Event(item_completed=event_pb2.ItemCompleted(item_id="replaced", text="Second replaced")),
            )
            rows = await _rows(engine, thread)
            replaced, kept = reference(rows, "replaced"), reference(rows, "kept")
            assert replaced["generation"] != first_generation

            chunks = _thread_shape("thread_payload_chunk", thread, field="text")
            async with asyncio.timeout(120), httpx.AsyncClient(base_url=service.url, timeout=35) as client:
                handle, start = await _open(client, chunks)

                def body(rows: list[Row], owner: str) -> str:
                    ordered = sorted(
                        (row for row in rows if row["owner_id"] == owner), key=lambda row: int(row["chunk_index"])
                    )
                    return "".join(row["text"] for row in ordered)

                # The bodies of the rows in view, in one request: each owner at the generation its
                # reference names, so a replaced body's old chunks stay behind.
                current, _ = await _subset(
                    client,
                    chunks,
                    handle,
                    start,
                    {
                        "where": "(owner_id = $1 AND generation = $2) OR (owner_id = $3 AND generation = $4)",
                        "params": {
                            "1": replaced["owner_id"],
                            "2": str(replaced["generation"]),
                            "3": kept["owner_id"],
                            "4": str(kept["generation"]),
                        },
                    },
                )
                assert {row["owner_id"] for row in current} == {replaced["owner_id"], kept["owner_id"]}
                assert body(current, replaced["owner_id"]) == "Second replaced"
                assert body(current, kept["owner_id"]) == "First kept"

                # An array parameter selects by owner alone — and so brings every generation.
                by_owner, _ = await _subset(
                    client,
                    chunks,
                    handle,
                    start,
                    {"where": "owner_id = ANY($1)", "params": {"1": f"{{{replaced['owner_id']},{kept['owner_id']}}}"}},
                )
                assert {row["owner_id"] for row in by_owner} == {replaced["owner_id"], kept["owner_id"]}
                assert {int(row["generation"]) for row in by_owner if row["owner_id"] == replaced["owner_id"]} == {
                    int(first_generation),
                    int(replaced["generation"]),
                }
        finally:
            await engine.dispose()


if __name__ == "__main__":
    pytest_bazel.main()
