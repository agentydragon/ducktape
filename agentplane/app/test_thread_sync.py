"""Real materialization and Electric replication using production migrations and role grants."""

import asyncio
import json
from collections.abc import Callable
from datetime import timedelta
from typing import Any
from uuid import UUID

import httpx
import pytest_bazel

from agentplane.app.agent_runtime.events.event_log import EventLogStore
from agentplane.app.agent_runtime.events.ingestion_lease import IngestionLease
from agentplane.app.agent_runtime.ingestion import Ingestion
from agentplane.app.database import connect
from agentplane.app.testing.electric_service import ElectricService, electric_service
from agentplane.app.testing.replication_process import app_process
from agentplane.app.testing.replication_source import SANDBOX, SESSION, ReplicationSource
from agentplane.protocol import command_pb2, event_pb2

# gazelle:include_dep @pypi//protobuf


async def test_materialized_revisions_replicate_with_restricted_role() -> None:
    async with electric_service() as service:
        engine = connect(service.database_url)
        event_logs, ingestion = EventLogStore(engine), Ingestion(engine)
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
            thread = await event_logs.open(SANDBOX, SESSION, source.attached.spec)
            lease = await ingestion.acquire(SANDBOX, timedelta(minutes=2))
            assert lease is not None
            await ingestion.set_attached(thread, source.attached, lease=lease)
            await ingestion.record(thread, source.entries, lease=lease)
            async with asyncio.timeout(45), httpx.AsyncClient(base_url=service.url, timeout=35) as client:
                params = {"table": "thread_entity", "where": f"thread_id = '{thread}'", "offset": "-1"}
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
                await ingestion.record(thread, source.entries[4:], lease=lease)
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
                        "table": "thread_payload_chunk",
                        "where": f"thread_id = '{thread}' AND owner_id = 'first'",
                        "offset": "-1",
                    },
                )
                chunks.raise_for_status()
                values = [message["value"] for message in chunks.json() if "value" in message]
                ordered = sorted(values, key=lambda row: int(row["chunk_index"]))
                assert "".join(row["text"] for row in ordered) == "Hello world"
            await _cross_replica_sync(service, ingestion, source, thread, lease)
        finally:
            await engine.dispose()


Row = dict[str, Any]


def _decoded(value: str | dict[str, Any]) -> dict[str, Any]:
    return json.loads(value) if isinstance(value, str) else value


async def _open(client: httpx.AsyncClient, path: str, epoch: str) -> tuple[str, str]:
    """A shape from now: no rows, only where its live log starts. Its window is loaded as subsets."""
    start = await client.get(path, params={"projection_epoch": epoch, "offset": "now"})
    start.raise_for_status()
    return start.headers["electric-handle"], start.headers["electric-offset"]


async def _subset(
    client: httpx.AsyncClient, path: str, epoch: str, shape: tuple[str, str], body: dict[str, object]
) -> tuple[list[Row], httpx.Response]:
    handle, offset = shape
    response = await client.post(
        path, params={"projection_epoch": epoch, "handle": handle, "offset": offset}, json=body
    )
    assert response.status_code == 200, response.text
    return [message["value"] for message in response.json()["data"] if "value" in message], response


async def _follow(
    client: httpx.AsyncClient, path: str, epoch: str, shape: tuple[str, str], done: Callable[[list[Row]], bool]
) -> tuple[list[Row], str]:
    """Changes on a shape's live log from its offset until `done` says enough arrived, and the text
    of every response that carried them."""
    handle, offset = shape
    changes: list[Row] = []
    texts: list[str] = []
    while not done(changes):
        response = await client.get(
            path, params={"projection_epoch": epoch, "handle": handle, "offset": offset, "live": "true"}
        )
        response.raise_for_status()
        offset = response.headers["electric-offset"]
        texts.append(response.text)
        changes.extend(message["value"] for message in response.json() if "value" in message)
    return changes, "".join(texts)


def _body(chunks: list[Row], reference: dict[str, Any]) -> str:
    """The body as far as the reference spans it."""
    texts = {
        int(row["chunk_index"]): row["text"]
        for row in chunks
        if row["owner_id"] == reference["owner_id"] and str(row["generation"]) == str(reference["generation"])
    }
    return "".join(texts[index] for index in range(int(reference["chunk_count"])))


def _extends(chunk: Row, reference: dict[str, Any]) -> bool:
    """Whether a chunk is the one after the last a reference spans."""
    return (
        chunk["owner_id"] == reference["owner_id"]
        and str(chunk["generation"]) == str(reference["generation"])
        and int(chunk["chunk_index"]) == int(reference["chunk_count"])
    )


def _bodies(*references: dict[str, Any]) -> dict[str, object]:
    return {
        "where": " OR ".join(
            f"(owner_id = ${2 * n - 1} AND generation = ${2 * n})" for n in range(1, len(references) + 1)
        ),
        "params": {
            key: value
            for n, reference in enumerate(references, 1)
            for key, value in ((str(2 * n - 1), reference["owner_id"]), (str(2 * n), str(reference["generation"])))
        },
    }


async def _cross_replica_sync(
    service: ElectricService, ingestion: Ingestion, source: ReplicationSource, thread: UUID, lease: IngestionLease
) -> None:
    async with (
        asyncio.timeout(60),
        app_process(service.database_url, runner_port=0, sandbox_state=None, electric_url=service.url) as first,
        app_process(service.database_url, runner_port=0, sandbox_state=None, electric_url=service.url) as second,
        httpx.AsyncClient(base_url=first.url, timeout=35) as client_one,
        httpx.AsyncClient(base_url=second.url, timeout=35) as client_two,
    ):
        path = f"/threads/{thread}/sync"
        scope = await client_one.get(f"{path}/scope")
        scope.raise_for_status()
        epoch = scope.json()["projection_epoch"]
        entities = await _open(client_one, f"{path}/entities", epoch)
        rows, tail = await _subset(
            client_one, f"{path}/entities", epoch, entities, {"order_by": "entity_index DESC", "limit": 30}
        )
        assert tail.headers["cache-control"] == "private, no-cache"
        reference = _decoded(next(row for row in rows if row["entity_id"] == "first")["text_ref"])
        chunks = await _open(client_one, f"{path}/chunks/text", epoch)
        before, _ = await _subset(client_one, f"{path}/chunks/text", epoch, chunks, _bodies(reference))
        assert _body(before, reference) == "Hello world"

        entry = source.append(event_pb2.Event(text_delta=event_pb2.TextDelta(item_id="first", text="!")))
        await ingestion.record(thread, [entry], lease=lease)
        # Both shapes resume at their handle and offset through a different application process.
        revised, _ = await _follow(
            client_two,
            f"{path}/entities",
            epoch,
            entities,
            lambda seen: any(
                row["entity_id"] == "first" and str(row["revision_cursor"]) == str(entry.cursor) for row in seen
            ),
        )
        extended = _decoded(next(row for row in reversed(revised) if row["entity_id"] == "first")["text_ref"])
        # A streamed delta costs the reader the delta: one new chunk, and a reference that spans it.
        appended, _ = await _follow(
            client_two, f"{path}/chunks/text", epoch, chunks, lambda seen: any(_extends(row, reference) for row in seen)
        )
        assert [row["text"] for row in appended if _extends(row, reference)] == ["!"]
        assert _body(before + appended, extended) == "Hello world!"
        # The rows a reader already holds still render the revision they name.
        assert _body(before + appended, reference) == "Hello world"

        stale = await client_two.get(f"{path}/chunks/text", params={"projection_epoch": "stale", "offset": "now"})
        assert stale.status_code == 410
        await _history_window(client_one, client_two, path, epoch, ingestion, source, thread, lease)
        await _command_outcome(client_one, client_two, path, epoch, ingestion, source, thread, lease)


async def _command_outcome(
    client_one: httpx.AsyncClient,
    client_two: httpx.AsyncClient,
    path: str,
    epoch: str,
    ingestion: Ingestion,
    source: ReplicationSource,
    thread: UUID,
    lease: IngestionLease,
) -> None:
    entities = await _open(client_one, f"{path}/entities", epoch)
    selected = {
        "where": "entity_kind = 'command' AND entity_id = ANY($1)",
        "params": {"1": "{selected-command}"},
        "order_by": "entity_index DESC",
        "limit": 1,
    }
    waiting, _ = await _subset(client_one, f"{path}/entities", epoch, entities, selected)
    assert waiting == []
    start = len(source.entries)
    for command_id in ("selected-command", "other-command"):
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
    await ingestion.record(thread, source.entries[start:], lease=lease)

    def failed(rows: list[Row]) -> bool:
        return any(
            row["entity_id"] == "selected-command" and _decoded(row["state"]).get("outcome") == "failed" for row in rows
        )

    changes, texts = await _follow(client_two, f"{path}/entities", epoch, entities, failed)
    outcome = _decoded(next(row for row in reversed(changes) if row["entity_id"] == "selected-command")["state"])
    assert outcome["outcome_reason"] == "Harness rejected input"
    # A command row carries its outcome; what the operator sent stays out of the thread's shape.
    assert "Private command body" not in texts
    # A reader that reloads with the command in flight finds its outcome by id.
    reloaded, _ = await _subset(client_two, f"{path}/entities", epoch, entities, selected)
    assert failed(reloaded)
    assert {row["entity_id"] for row in reloaded} == {"selected-command"}


async def _history_window(
    client_one: httpx.AsyncClient,
    client_two: httpx.AsyncClient,
    path: str,
    epoch: str,
    ingestion: Ingestion,
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
    await ingestion.record(thread, source.entries[start:], lease=lease)

    # A reader arriving now loads the tail, however long the thread has grown.
    entities = await _open(client_two, f"{path}/entities", epoch)
    held, tail = await _subset(
        client_two, f"{path}/entities", epoch, entities, {"order_by": "entity_index DESC", "limit": 30}
    )
    assert {row["entity_id"] for row in held} == {f"history-{index:03}" for index in range(65, 95)}
    assert "Body 94" not in tail.text

    # Scrolling up loads the page before the oldest held row, and the tail stays held.
    for lower in (35, 5):
        page, _ = await _subset(
            client_one,
            f"{path}/entities",
            epoch,
            entities,
            {
                "where": "entity_index < $1",
                "params": {"1": str(min(int(row["entity_index"]) for row in held))},
                "order_by": "entity_index DESC",
                "limit": 30,
            },
        )
        assert {row["entity_id"] for row in page} == {f"history-{index:03}" for index in range(lower, lower + 30)}
        held += page

    # A row loaded by scrolling is as live as the tail: its edits are on the same log.
    entry = source.append(event_pb2.Event(text_delta=event_pb2.TextDelta(item_id="history-010", text=" revised")))
    hidden_text = "A whole selected tool-sized body.\n" * 65536
    hidden = source.append(event_pb2.Event(text_delta=event_pb2.TextDelta(item_id="history-040", text=hidden_text)))
    await ingestion.record(thread, [entry], lease=lease)
    await ingestion.record(thread, [hidden], lease=lease)

    def revised(seen: list[Row], entity_id: str, cursor: int) -> Row | None:
        return next(
            (row for row in seen if row["entity_id"] == entity_id and str(row["revision_cursor"]) == str(cursor)), None
        )

    # The log may also carry the rows' first inserts, replicated after the shape opened.
    changes, texts = await _follow(
        client_one,
        f"{path}/entities",
        epoch,
        entities,
        lambda seen: (
            revised(seen, "history-010", entry.cursor) is not None
            and revised(seen, "history-040", hidden.cursor) is not None
        ),
    )
    # Rows carry references; a body loads only when a reader asks for it.
    assert "A whole selected tool-sized body" not in texts
    latest = revised(changes, "history-040", hidden.cursor)
    assert latest is not None
    reference = _decoded(latest["text_ref"])
    chunks = await _open(client_two, f"{path}/chunks/text", epoch)
    body, _ = await _subset(client_two, f"{path}/chunks/text", epoch, chunks, _bodies(reference))
    assert _body(body, reference) == "Body 40" + hidden_text


if __name__ == "__main__":
    pytest_bazel.main()
