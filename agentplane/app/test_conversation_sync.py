"""Real materialization and Electric replication using production migrations and role grants."""

import asyncio
import json
from datetime import timedelta
from uuid import UUID

import httpx
import pytest_bazel

from agentplane.app.testing.electric_service import ElectricService, electric_service
from agentplane.app.testing.replication_process import app_process
from agentplane.app.testing.replication_source import SANDBOX, SESSION, ReplicationSource
from agentplane.app.trajectory import IngestionLease, TrajectoryStore
from agentplane.protocol import event_pb2

# gazelle:include_dep @pypi//protobuf


async def test_materialized_revisions_replicate_with_restricted_role() -> None:
    async with electric_service() as service:
        store = TrajectoryStore.connect(service.database_url)
        try:
            source = ReplicationSource()
            source.append(event_pb2.Event(harness_started=event_pb2.HarnessStarted(pid=123)))
            source.append(event_pb2.Event(turn_started=event_pb2.TurnStarted(turn_id="turn", model="test-model")))
            source.append(
                event_pb2.Event(
                    item_started=event_pb2.ItemStarted(item_id="first", kind=event_pb2.ITEM_KIND_ASSISTANT_TEXT)
                )
            )
            source.append(event_pb2.Event(text_delta=event_pb2.TextDelta(item_id="first", text="Hello")))
            thread = await store.thread(SANDBOX, SESSION, source.attached.spec)
            lease = await store.acquire_ingestion(SANDBOX, timedelta(minutes=2))
            assert lease is not None
            await store.set_attached(thread, source.attached, lease=lease)
            await store.record(thread, source.entries, lease=lease)
            async with asyncio.timeout(45), httpx.AsyncClient(base_url=service.url, timeout=35) as client:
                params = {"table": "conversation_entity", "where": f"thread_id = '{thread}'", "offset": "-1"}
                initial = await client.get("/v1/shape", params=params)
                assert initial.status_code == 200, initial.text
                entities = [message["value"] for message in initial.json() if "value" in message]
                first = next(row for row in entities if row["entity_id"] == "first")
                assert str(first["revision_cursor"]) == "4"
                offset = initial.headers["electric-offset"]
                handle = initial.headers["electric-handle"]
                # A newer item does not prevent the earlier item from receiving a revision.
                source.append(
                    event_pb2.Event(
                        item_started=event_pb2.ItemStarted(item_id="second", kind=event_pb2.ITEM_KIND_ASSISTANT_TEXT)
                    )
                )
                source.append(event_pb2.Event(text_delta=event_pb2.TextDelta(item_id="first", text=" world")))
                await store.record(thread, source.entries[4:], lease=lease)
                seen_revision = False
                while not seen_revision:
                    delta = await client.get(
                        "/v1/shape", params=params | {"offset": offset, "handle": handle, "live": "true"}
                    )
                    delta.raise_for_status()
                    offset = delta.headers["electric-offset"]
                    for message in delta.json():
                        value = message.get("value", {})
                        if value.get("entity_id") == "first" and str(value.get("revision_cursor")) == "6":
                            seen_revision = True
                chunks = await client.get(
                    "/v1/shape",
                    params={
                        "table": "conversation_payload_chunk",
                        "where": f"thread_id = '{thread}' AND owner_id = 'first'",
                        "offset": "-1",
                    },
                )
                chunks.raise_for_status()
                values = [message["value"] for message in chunks.json() if "value" in message]
                ordered = sorted(values, key=lambda row: int(row["chunk_index"]))
                assert "".join(row["text"] for row in ordered) == "Hello world"
            await _cross_replica_sync(service, store, source, thread, lease)
        finally:
            await store.close()


async def _cross_replica_sync(
    service: ElectricService, store: TrajectoryStore, source: ReplicationSource, thread: UUID, lease: IngestionLease
) -> None:
    async with (
        asyncio.timeout(60),
        app_process(service.database_url, "unused", sandbox_state=None, electric_url=service.url) as first,
        app_process(service.database_url, "unused", sandbox_state=None, electric_url=service.url) as second,
        httpx.AsyncClient(base_url=first.url, timeout=35) as client_one,
        httpx.AsyncClient(base_url=second.url, timeout=35) as client_two,
    ):
        path = f"/threads/{thread}/sync"
        interest = await client_one.get(f"{path}/interest")
        interest.raise_for_status()
        selection = interest.json()
        entity_params = {"anchor_cursor": selection["anchor_cursor"], "tail_from": selection["tail_from"]}
        initial = await client_one.get(f"{path}/entities", params=entity_params | {"offset": "-1"})
        assert initial.status_code == 200, initial.text
        rows = [message["value"] for message in initial.json() if "value" in message]
        first_item = next(row for row in rows if row["entity_id"] == "first")
        raw_ref = first_item["text_ref"]
        reference = json.loads(raw_ref) if isinstance(raw_ref, str) else raw_ref
        payload_params = {
            "source_id": reference["source_id"],
            "projection_epoch": reference["projection_epoch"],
            "owner_cursor": reference["owner_cursor"],
            "owner_id": reference["owner_item_id"],
            "field": reference["field"],
            "generation": reference["generation"],
            "revision_cursor": reference["revision_cursor"],
        }
        body_before = await client_one.get(
            f"{path}/payload-chunks", params=payload_params | {"offset": "-1", "follow": "true"}
        )
        body_before.raise_for_status()
        before_chunks = [message["value"] for message in body_before.json() if "value" in message]
        assert (
            "".join(row["text"] for row in sorted(before_chunks, key=lambda row: int(row["chunk_index"])))
            == "Hello world"
        )
        entry = source.append(event_pb2.Event(text_delta=event_pb2.TextDelta(item_id="first", text="!")))
        await store.record(thread, [entry], lease=lease)
        # Resume the same shape handle/offset through a different application process.
        metadata_offset = initial.headers["electric-offset"]
        while True:
            changed = await client_two.get(
                f"{path}/entities",
                params=entity_params
                | {"offset": metadata_offset, "handle": initial.headers["electric-handle"], "live": "true"},
            )
            changed.raise_for_status()
            metadata_offset = changed.headers["electric-offset"]
            if any(
                message.get("value", {}).get("entity_id") == "first"
                and str(message.get("value", {}).get("revision_cursor")) == str(entry.cursor)
                for message in changed.json()
            ):
                break
        offset = body_before.headers["electric-offset"]
        while True:
            changed_body = await client_two.get(
                f"{path}/payload-chunks",
                params=payload_params
                | {
                    "offset": offset,
                    "handle": body_before.headers["electric-handle"],
                    "live": "true",
                    "follow": "true",
                },
            )
            changed_body.raise_for_status()
            offset = changed_body.headers["electric-offset"]
            chunks = [message["value"] for message in changed_body.json() if "value" in message]
            if chunks:
                assert [row["text"] for row in chunks] == ["!"]
                break
        # A pinned R read made after R+1 committed reconstructs exactly the old whole body.
        pinned = await client_two.get(f"{path}/payload-chunks", params=payload_params | {"offset": "-1"})
        pinned.raise_for_status()
        old_chunks = [message["value"] for message in pinned.json() if "value" in message]
        assert (
            "".join(row["text"] for row in sorted(old_chunks, key=lambda row: int(row["chunk_index"]))) == "Hello world"
        )
        stale = await client_two.get(f"{path}/payload-interest", params=payload_params | {"projection_epoch": "stale"})
        assert stale.status_code == 410


if __name__ == "__main__":
    pytest_bazel.main()
