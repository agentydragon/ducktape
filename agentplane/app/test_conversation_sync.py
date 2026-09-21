"""Real materialization and Electric replication using production migrations and role grants."""

import asyncio
from datetime import timedelta

import httpx
import pytest_bazel

from agentplane.app.testing.electric_service import electric_service
from agentplane.app.testing.replication_source import SANDBOX, SESSION, ReplicationSource
from agentplane.app.trajectory import TrajectoryStore
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
                initial.raise_for_status()
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
        finally:
            await store.close()


if __name__ == "__main__":
    pytest_bazel.main()
