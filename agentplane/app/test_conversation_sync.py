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
from agentplane.protocol import command_pb2, event_pb2

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


async def _current_snapshot(client: httpx.AsyncClient, path: str, params: dict[str, str]) -> httpx.Response:
    start = await client.get(path, params=params | {"offset": "now"})
    start.raise_for_status()
    snapshot = await client.get(
        path,
        params=params
        | {
            "offset": start.headers["electric-offset"],
            "handle": start.headers["electric-handle"],
            "subset__where": "true = true",
        },
    )
    snapshot.raise_for_status()
    assert isinstance(snapshot.json()["data"], list), snapshot.text
    assert isinstance(snapshot.json()["metadata"], dict), snapshot.text
    # These tests serialize writes after snapshot completion. The browser tests
    # exercise TanStack's transaction-aware snapshot/live reconciliation.
    snapshot.headers["electric-offset"] = start.headers["electric-offset"]
    snapshot.headers["electric-handle"] = start.headers["electric-handle"]
    return snapshot


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
        entity_params = {key: selection[key] for key in ("source_id", "projection_epoch", "anchor_cursor", "tail_from")}
        initial = await _current_snapshot(client_one, f"{path}/entities", entity_params)
        assert initial.status_code == 200, initial.text
        rows = [message["value"] for message in initial.json()["data"] if "value" in message]
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
        }
        body_params = payload_params | {"revision_cursor": reference["revision_cursor"]}
        body_before = await client_one.get(f"{path}/payload-chunks", params=payload_params | {"offset": "-1"})
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
                | {"offset": offset, "handle": body_before.headers["electric-handle"], "live": "true"},
            )
            changed_body.raise_for_status()
            offset = changed_body.headers["electric-offset"]
            chunks = [message["value"] for message in changed_body.json() if "value" in message]
            if chunks:
                assert [row["text"] for row in chunks] == ["!"]
                break
        # The same shape served both reads: an appended revision extends it rather than defining
        # another, so resuming its handle is what delivered "!" above and no second handle exists.
        resumed = await client_two.get(f"{path}/payload-chunks", params=payload_params | {"offset": "-1"})
        resumed.raise_for_status()
        assert resumed.headers["electric-handle"] == body_before.headers["electric-handle"]

        # A pinned R read made after R+1 committed still reconstructs exactly the old whole body,
        # now over the bounded HTTP route rather than a shape narrowed to that revision's prefix.
        pinned = await client_two.get(f"/threads/{thread}/conversation/payload", params=body_params)
        pinned.raise_for_status()
        assert pinned.json() == {"availability": "present", "body": "Hello world"}
        assert pinned.headers["cache-control"] == "private, no-cache"
        revalidated = await client_two.get(
            f"/threads/{thread}/conversation/payload",
            params=body_params,
            headers={"if-none-match": pinned.headers["etag"]},
        )
        assert (revalidated.status_code, revalidated.content) == (304, b"")

        current = await client_two.get(
            f"/threads/{thread}/conversation/payload", params=payload_params | {"revision_cursor": str(entry.cursor)}
        )
        current.raise_for_status()
        assert current.json()["body"] == "Hello world!"

        stale = await client_two.get(f"{path}/payload-chunks", params=payload_params | {"projection_epoch": "stale"})
        assert stale.status_code == 410
        stale_body = await client_two.get(
            f"/threads/{thread}/conversation/payload", params=body_params | {"projection_epoch": "stale"}
        )
        assert stale_body.status_code == 410
        await _history_windows(client_one, client_two, path, entity_params, store, source, thread, lease)
        await _selected_command_outcome(client_one, client_two, path, store, source, thread, lease)


async def _selected_command_outcome(
    client_one: httpx.AsyncClient,
    client_two: httpx.AsyncClient,
    path: str,
    store: TrajectoryStore,
    source: ReplicationSource,
    thread: UUID,
    lease: IngestionLease,
) -> None:
    scope = await store.current_conversation_scope(thread)
    assert scope is not None
    params = {
        "source_id": scope.source_id,
        "projection_epoch": scope.projection_epoch,
        "command_id": "selected-command",
    }
    snapshot = await _current_snapshot(client_one, f"{path}/commands", params)
    snapshot.raise_for_status()
    assert not [message for message in snapshot.json()["data"] if "value" in message]
    start = len(source.entries)
    for command_id in ("selected-command", "unselected-command"):
        source.append(
            event_pb2.Event(
                command_admitted=event_pb2.CommandAdmitted(
                    command=command_pb2.Command(
                        command_id=command_id, submit_input=command_pb2.SubmitInput(text="Private command body")
                    )
                )
            )
        )
        source.append(
            event_pb2.Event(
                command_failed=event_pb2.CommandFailed(command_id=command_id, reason="Harness rejected input")
            )
        )
    # Admission and failure may coalesce before the browser observes any pending row.
    await store.record(thread, source.entries[start:], lease=lease)
    offset = snapshot.headers["electric-offset"]
    while True:
        update = await client_two.get(
            f"{path}/commands",
            params=params | {"offset": offset, "handle": snapshot.headers["electric-handle"], "live": "true"},
        )
        update.raise_for_status()
        offset = update.headers["electric-offset"]
        rows = [message["value"] for message in update.json() if "value" in message]
        if rows:
            assert {row["entity_id"] for row in rows} == {"selected-command"}
            state = json.loads(rows[-1]["state"]) if isinstance(rows[-1]["state"], str) else rows[-1]["state"]
            assert state["outcome"] == "failed"
            assert state["outcome_reason"] == "Harness rejected input"
            assert "Private command body" not in update.text
            break


async def _history_windows(
    client_one: httpx.AsyncClient,
    client_two: httpx.AsyncClient,
    path: str,
    previous_params: dict[str, str],
    store: TrajectoryStore,
    source: ReplicationSource,
    thread: UUID,
    lease: IngestionLease,
) -> None:
    start = len(source.entries)
    for index in range(95):
        source.append(
            event_pb2.Event(
                item_started=event_pb2.ItemStarted(
                    item_id=f"history-{index:03}", kind=event_pb2.ITEM_KIND_ASSISTANT_TEXT
                )
            )
        )
        source.append(
            event_pb2.Event(text_delta=event_pb2.TextDelta(item_id=f"history-{index:03}", text=f"Body {index}"))
        )
    await store.record(thread, source.entries[start:], lease=lease)

    # A disconnected client refreshes its interest instead of replaying an unbounded backlog.
    expired = await client_two.get(f"{path}/entities", params=previous_params | {"offset": "-1"})
    assert expired.status_code == 410
    tail = await client_two.get(f"{path}/interest")
    tail.raise_for_status()
    tail_interest = tail.json()
    tail_params = {key: tail_interest[key] for key in ("source_id", "projection_epoch", "anchor_cursor", "tail_from")}
    snapshot = await _current_snapshot(client_two, f"{path}/entities", tail_params)
    snapshot.raise_for_status()
    assert snapshot.headers["cache-control"] == "private, no-store"
    rows = [message["value"] for message in snapshot.json()["data"] if "value" in message]
    assert {row["entity_id"] for row in rows if row["entity_kind"] == "item"} == {
        f"history-{index:03}" for index in range(65, 95)
    }
    assert "Body 94" not in snapshot.text

    # Keep the live tail while replacing the bounded historical window on each scroll.
    before = tail_interest["tail_from"]
    for lower in (35, 5):
        selected = await client_one.get(f"{path}/interest", params={"before_cursor": before})
        selected.raise_for_status()
        interest = selected.json()
        window_params = {
            key: interest[key]
            for key in ("source_id", "projection_epoch", "anchor_cursor", "tail_from", "window_from", "window_before")
        }
        page = await _current_snapshot(client_two, f"{path}/entities", window_params)
        page.raise_for_status()
        page_rows = [message["value"] for message in page.json()["data"] if "value" in message]
        assert {row["entity_id"] for row in page_rows if row["entity_kind"] == "item"} == {
            f"history-{index:03}" for index in (*range(lower, lower + 30), *range(65, 95))
        }
        before = interest["window_from"]

    # An item inside the history window remains live even while newer items exist.
    entry = source.append(event_pb2.Event(text_delta=event_pb2.TextDelta(item_id="history-010", text=" revised")))
    await store.record(thread, [entry], lease=lease)
    offset = page.headers["electric-offset"]
    while True:
        revision = await client_one.get(
            f"{path}/entities",
            params=window_params | {"offset": offset, "handle": page.headers["electric-handle"], "live": "true"},
        )
        revision.raise_for_status()
        offset = revision.headers["electric-offset"]
        if any(
            message.get("value", {}).get("entity_id") == "history-010"
            and str(message.get("value", {}).get("revision_cursor")) == str(entry.cursor)
            for message in revision.json()
        ):
            break

    hidden_text = "A whole selected tool-sized body.\n" * 65536
    hidden = source.append(event_pb2.Event(text_delta=event_pb2.TextDelta(item_id="history-040", text=hidden_text)))
    visible = source.append(event_pb2.Event(text_delta=event_pb2.TextDelta(item_id="history-094", text=" visible")))
    await store.record(thread, [hidden, visible], lease=lease)
    while True:
        update = await client_two.get(
            f"{path}/entities",
            params=window_params | {"offset": offset, "handle": page.headers["electric-handle"], "live": "true"},
        )
        update.raise_for_status()
        offset = update.headers["electric-offset"]
        values = [message["value"] for message in update.json() if "value" in message]
        assert all(row.get("entity_id") != "history-040" for row in values)
        assert "A whole selected tool-sized body" not in update.text
        if any(row.get("entity_id") == "history-094" for row in values):
            break

    # Revisit an evicted window; its latest metadata selects the complete large body on demand.
    revisit = await client_two.get(f"{path}/interest", params={"before_cursor": tail_interest["tail_from"]})
    revisit.raise_for_status()
    revisit_interest = revisit.json()
    revisit_params = {
        key: revisit_interest[key]
        for key in ("source_id", "projection_epoch", "anchor_cursor", "tail_from", "window_from", "window_before")
    }
    revisit_page = await _current_snapshot(client_one, f"{path}/entities", revisit_params)
    versions = [
        message["value"]
        for message in revisit_page.json()["data"]
        if message.get("value", {}).get("entity_id") == "history-040"
    ]
    assert len(versions) == 1
    item = versions[0]
    assert str(item["revision_cursor"]) == str(hidden.cursor)
    raw_ref = item["text_ref"]
    reference = json.loads(raw_ref) if isinstance(raw_ref, str) else raw_ref
    # Revisiting an evicted window reads completed bodies the way the browser does: whole, at the
    # exact revision the re-fetched row names, with no shape for content that can no longer change.
    selected = await client_one.get(
        f"/threads/{thread}/conversation/payload",
        params={
            "source_id": reference["source_id"],
            "projection_epoch": reference["projection_epoch"],
            "owner_cursor": reference["owner_cursor"],
            "owner_id": reference["owner_item_id"],
            "field": reference["field"],
            "generation": reference["generation"],
            "revision_cursor": reference["revision_cursor"],
        },
    )
    selected.raise_for_status()
    assert selected.json() == {"availability": "present", "body": "Body 40" + hidden_text}


if __name__ == "__main__":
    pytest_bazel.main()
