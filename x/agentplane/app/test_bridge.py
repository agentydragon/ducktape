"""One browser-shaped script over the bridge against a local runner, run for both harnesses: open a
session, stream it, send an input while streaming, open a second tab on the same session, reconnect
from the last event id, shut down; and the trajectory the store kept of all of it."""

from __future__ import annotations

import asyncio
import json
import socket
from collections.abc import AsyncIterator
from contextlib import aclosing
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
import pytest_bazel
import uvicorn
from google.protobuf.json_format import MessageToDict
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_delay, wait_fixed

from x.agentplane.app.api import Provider, create_app
from x.agentplane.app.bridge import RunnerBridge
from x.agentplane.app.changes import Changes
from x.agentplane.app.conftest import AGENT_AUTH
from x.agentplane.app.decisions import DecisionsClient
from x.agentplane.app.egress import EgressInventory
from x.agentplane.app.identity import TokenReviewer
from x.agentplane.app.inventory import SandboxInventory
from x.agentplane.app.live import LiveIndex
from x.agentplane.app.trajectory import TrajectoryStore
from x.agentplane.runner import protocol_pb2 as pb
from x.agentplane.runner.client import RunnerClient
from x.agentplane.runner.conftest import RunnerHandle
from x.agentplane.runner.testing.scripted_model import ScriptedModel, Text

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf

SANDBOX = "bridge-test-sandbox"
SESSION = "bridge-1"
SESSIONS = f"/sandboxes/{SANDBOX}/sessions"
EVENTS = f"{SESSIONS}/{SESSION}/events"


@dataclass(frozen=True)
class SseMessage:
    event: str
    id: int | None
    data: dict[str, Any]


async def next_message(lines: AsyncIterator[str]) -> SseMessage:
    """The next SSE message; comments (keepalives) are skipped."""
    event, event_id, data = "", None, ""
    async for line in lines:
        if line == "":
            if event:
                return SseMessage(event, event_id, json.loads(data))
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(": ")
        match field:
            case "event":
                event = value
            case "id":
                event_id = int(value)
            case "data":
                data = value
    raise AssertionError("the stream ended without a message")


async def read_until(lines: AsyncIterator[str], key: str) -> list[SseMessage]:
    """Events up to and including the first whose payload carries `key`."""
    seen: list[SseMessage] = []
    while True:
        message = await next_message(lines)
        seen.append(message)
        if key in message.data:
            return seen


@pytest.fixture
async def app_url(
    runner: RunnerHandle,
    inventory: SandboxInventory,
    store: TrajectoryStore,
    egress: EgressInventory,
    decisions: DecisionsClient,
    live_index: LiveIndex,
    reviewer: TokenReviewer,
) -> AsyncIterator[str]:
    """The app served by uvicorn, with the one test sandbox resolving to the local runner. The
    server is real because SSE needs a response that streams, which an in-process ASGI transport
    would buffer."""

    async def address_of(name: str) -> str:
        assert name == SANDBOX
        return runner.target

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])
    bridge = RunnerBridge(address_of=address_of, store=store)
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(
                inventory,
                bridge,
                store,
                {provider: ["bridge-model"] for provider in Provider},
                egress,
                decisions,
                live_index,
                reviewer=reviewer,
            ),
            host="127.0.0.1",
            port=port,
            log_level="warning",
        )
    )
    serving = asyncio.create_task(server.serve())
    async for attempt in AsyncRetrying(
        stop=stop_after_delay(30), wait=wait_fixed(0.1), retry=retry_if_exception_type(OSError)
    ):
        with attempt:
            _, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.close()
            await writer.wait_closed()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await serving
        await bridge.close()


async def test_the_bridge_streams_a_turn_to_every_tab_and_resumes_from_the_last_event_id(
    app_url: str, model: ScriptedModel, spec: pb.SessionSpec
) -> None:
    async with httpx.AsyncClient(base_url=app_url, timeout=60, headers=AGENT_AUTH) as http:
        opened = await http.post(SESSIONS, json={"session_id": SESSION, "spec": MessageToDict(spec)})
        assert opened.status_code == 201, opened.text
        assert opened.json()["harness"] == "HARNESS_STATE_RUNNING"
        assert [row["sessionId"] for row in (await http.get(SESSIONS)).json()] == [SESSION]

        async with http.stream("GET", EVENTS) as first_tab:
            first = first_tab.aiter_lines()
            assert (await next_message(first)).event == "attached"
            reopened = await http.post(SESSIONS, json={"session_id": SESSION, "spec": MessageToDict(spec)})
            assert reopened.status_code == 201, reopened.text
            accepted = await http.post(
                f"{SESSIONS}/{SESSION}/inputs", json={"inputId": "input-1", "text": "Reply with exactly: BRIDGE_OK"}
            )
            assert accepted.status_code == 202, accepted.text
            model.reply(await model.request(), Text("BRIDGE_OK"))
            seen = await read_until(first, "turnCompleted")
            # Every runner event, in order, from the start of the session's log: replay and live alike.
            assert [message.id for message in seen] == list(range(1, len(seen) + 1))
            assert all(message.event == "event" for message in seen)
            assert seen[-1].data["turnCompleted"]["status"] == "TURN_STATUS_COMPLETED"
            assert any("native" in message.data for message in seen)
            completed = [message.data["itemCompleted"] for message in seen if "itemCompleted" in message.data]
            assert [item["text"] for item in completed] == ["BRIDGE_OK"]

            # A second tab loads the first tab's history, then both follow the same committed log.
            async with http.stream("GET", EVENTS) as second_tab:
                second = second_tab.aiter_lines()
                assert (await next_message(second)).event == "attached"
                assert await read_until(second, "turnCompleted") == seen
                accepted = await http.post(
                    f"{SESSIONS}/{SESSION}/inputs",
                    json={"inputId": "input-2", "text": "Reply with exactly: BRIDGE_TWO"},
                )
                assert accepted.status_code == 202, accepted.text
                model.reply(await model.request(), Text("BRIDGE_TWO"))
                on_first, on_second = await asyncio.gather(
                    read_until(first, "turnCompleted"), read_until(second, "turnCompleted")
                )
                assert on_first == on_second
                assert on_first[0].id == len(seen) + 1
                assert [
                    message.data["itemCompleted"]["text"] for message in on_first if "itemCompleted" in message.data
                ] == ["BRIDGE_TWO"]

        # A browser reconnecting sends the last id it saw; the next event follows it without a gap.
        cut = seen[len(seen) // 2].id
        assert cut is not None
        async with http.stream("GET", EVENTS, headers={"Last-Event-ID": str(cut)}) as stream:
            lines = stream.aiter_lines()
            assert (await next_message(lines)).event == "attached"
            assert (await next_message(lines)).id == cut + 1

        stopped = await http.post(f"{SESSIONS}/{SESSION}/shutdown")
        assert stopped.status_code == 202, stopped.text
        (summary,) = (await http.get(SESSIONS)).json()
        assert summary["harness"] == "HARNESS_STATE_STOPPED"

        # The store kept the whole trajectory, readable without the runner: both turns, the raw
        # frames, and the exit the shutdown caused.
        (thread,) = (await http.get("/threads")).json()
        assert (thread["sandbox"], thread["session_id"], thread["model"]) == (SANDBOX, SESSION, spec.model)
        stored = await _stored_events(http, thread["id"], until="harnessExited")
        assert [event["sequence"] for event in stored] == [str(n) for n in range(1, len(stored) + 1)]
        assert [event["itemCompleted"]["text"] for event in stored if "itemCompleted" in event] == [
            "BRIDGE_OK",
            "BRIDGE_TWO",
        ]
        assert any("native" in event for event in stored)
        assert (await http.get(f"/threads/{thread['id']}")).json()["last_sequence"] == len(stored)
    model.assert_quiescent()


async def _stored_events(http: httpx.AsyncClient, thread_id: str, *, until: str) -> list[dict[str, Any]]:
    """The thread's stored events once one carrying `until` has landed; the feed writes them as
    they arrive, a moment after the runner emitted them."""
    async for attempt in AsyncRetrying(
        stop=stop_after_delay(30), wait=wait_fixed(0.2), retry=retry_if_exception_type(AssertionError)
    ):
        with attempt:
            response = await http.get(f"/threads/{thread_id}/events")
            assert response.status_code == 200, response.text
            events: list[dict[str, Any]] = response.json()
            assert any(until in event for event in events), f"no {until} stored yet"
    return events


async def test_the_feed_records_a_turn_nobody_is_watching(
    app_url: str, model: ScriptedModel, spec: pb.SessionSpec
) -> None:
    """Opening a session starts its feed, so a turn driven over REST alone lands in the store."""
    async with httpx.AsyncClient(base_url=app_url, timeout=60, headers=AGENT_AUTH) as http:
        opened = await http.post(SESSIONS, json={"session_id": "unwatched", "spec": MessageToDict(spec)})
        assert opened.status_code == 201, opened.text
        accepted = await http.post(
            f"{SESSIONS}/unwatched/inputs", json={"inputId": "input-1", "text": "Reply with exactly: UNWATCHED_OK"}
        )
        assert accepted.status_code == 202, accepted.text
        model.reply(await model.request(), Text("UNWATCHED_OK"))
        (thread,) = (await http.get("/threads")).json()
        stored = await _stored_events(http, thread["id"], until="turnCompleted")
        assert [event["itemCompleted"]["text"] for event in stored if "itemCompleted" in event] == ["UNWATCHED_OK"]
        assert (await http.post(f"{SESSIONS}/unwatched/shutdown")).status_code == 202
        assert (await http.get("/threads/00000000-0000-0000-0000-000000000000/events")).status_code == 404
    model.assert_quiescent()


async def test_the_bridge_reports_what_the_runner_refuses(app_url: str) -> None:
    async with httpx.AsyncClient(base_url=app_url, timeout=60, headers=AGENT_AUTH) as http:
        unknown = await http.post(f"{SESSIONS}/never-opened/inputs", json={"inputId": "x", "text": "hello"})
        assert unknown.status_code == 409
        assert "does not exist" in unknown.json()["detail"]
        malformed = await http.post(
            SESSIONS, json={"session_id": "s", "spec": {"provider": "PROVIDER_CLAUDE", "nope": 1}}
        )
        assert malformed.status_code == 422


@dataclass
class Replicas:
    owner: RunnerBridge
    survivor: RunnerBridge


@pytest.fixture
async def replicas(runner: RunnerHandle, store: TrajectoryStore, db_url: str) -> AsyncIterator[Replicas]:
    async def address_of(name: str) -> str:
        assert name == SANDBOX
        return runner.target

    replica_store = TrajectoryStore.connect(db_url)
    await replica_store.start_updates()
    owner = RunnerBridge(address_of=address_of, store=store)
    survivor = RunnerBridge(address_of=address_of, store=replica_store)
    await owner.start([SANDBOX])
    await owner.reconcile()
    try:
        yield Replicas(owner, survivor)
    finally:
        await owner.close()
        await survivor.close()
        await replica_store.close()


async def frame_lines(frames: AsyncIterator[bytes]) -> AsyncIterator[str]:
    async for frame in frames:
        for line in frame.decode().splitlines():
            yield line


async def test_replica_commands_and_database_stream_survive_ingestion_owner_exit(
    replicas: Replicas, model: ScriptedModel, spec: pb.SessionSpec
) -> None:
    await replicas.owner.open_session(SANDBOX, SESSION, spec)
    async with aclosing(replicas.survivor.events(SANDBOX, SESSION, after_sequence=0)) as frames:
        lines = frame_lines(frames)
        assert (await next_message(lines)).event == "attached"
        await replicas.survivor.send(SANDBOX, SESSION, pb.Input(input_id="input-1", text="FIRST_REPLICA_TURN"))
        model.reply(await model.request(), Text("FIRST_REPLICA_TURN"))
        async with asyncio.timeout(10):
            first = await read_until(lines, "turnCompleted")

        await replicas.survivor.send(SANDBOX, SESSION, pb.Input(input_id="input-2", text="AFTER_OWNER_EXIT"))
        request = await model.request()
        await replicas.owner.close()
        model.reply(request, Text("AFTER_OWNER_EXIT"))
        async with asyncio.timeout(10):
            second = await read_until(lines, "turnCompleted")
        seen = [*first, *second]
        assert [message.id for message in seen] == list(range(1, len(seen) + 1))
        assert [message.data["itemCompleted"]["text"] for message in seen if "itemCompleted" in message.data] == [
            "FIRST_REPLICA_TURN",
            "AFTER_OWNER_EXIT",
        ]
        await replicas.survivor.shutdown(SANDBOX, SESSION)
        async with asyncio.timeout(10):
            await read_until(lines, "harnessExited")
            assert (await next_message(lines)).event == "end"
    model.assert_quiescent()


async def test_inventory_change_discovers_existing_runner_session_without_browser_open(
    runner: RunnerHandle,
    store: TrajectoryStore,
    model: ScriptedModel,
    spec: pb.SessionSpec,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # This must wake from the informer notification, not the periodic recovery scan.
    monkeypatch.setattr("x.agentplane.app.bridge.RECONCILE_S", 3600)
    running: list[str] = []
    changes = Changes()
    discovered = asyncio.Event()

    async def address_of(name: str) -> str:
        assert name == SANDBOX
        return runner.target

    async def discover() -> list[str]:
        discovered.set()
        return list(running)

    bridge = RunnerBridge(address_of=address_of, store=store, discover_sandboxes=discover, sandbox_changes=changes)
    client = RunnerClient(runner.target)
    try:
        attachment = await client.attach(SESSION, spec=spec)
        await attachment.detach()
        await attachment.drain_until_end()
        await bridge.start([])
        async with asyncio.timeout(10):
            await discovered.wait()
        discovered.clear()
        running.append(SANDBOX)
        changes.notify()
        async with asyncio.timeout(10):
            await discovered.wait()
        # Only the discovery coordinator can create this row: no app Open, command, or SSE request ran.
        async for attempt in AsyncRetrying(
            stop=stop_after_delay(10), wait=wait_fixed(0.1), retry=retry_if_exception_type(AssertionError)
        ):
            with attempt:
                threads = await store.list_threads()
                assert len(threads) == 1
                assert await store.last_sequence(threads[0].id) > 0
        assert threads[0].sandbox == SANDBOX
        assert threads[0].session_id == SESSION
    finally:
        await bridge.close()
        await client.close()
    model.assert_quiescent()


async def test_resumed_session_stream_does_not_end_at_previous_shutdown(
    replicas: Replicas, model: ScriptedModel, spec: pb.SessionSpec
) -> None:
    await replicas.owner.open_session(SANDBOX, SESSION, spec)
    await replicas.survivor.send(SANDBOX, SESSION, pb.Input(input_id="seed-input", text="BEFORE_RESUME"))
    model.reply(await model.request(), Text("BEFORE_RESUME"))
    async with aclosing(replicas.survivor.events(SANDBOX, SESSION, after_sequence=0)) as frames:
        async with asyncio.timeout(10):
            await read_until(frame_lines(frames), "turnCompleted")
    await replicas.survivor.shutdown(SANDBOX, SESSION)
    async with aclosing(replicas.survivor.events(SANDBOX, SESSION, after_sequence=0)) as frames:
        lines = frame_lines(frames)
        assert (await next_message(lines)).event == "attached"
        async with asyncio.timeout(10):
            stopped = await read_until(lines, "harnessExited")
            assert (await next_message(lines)).event == "end"
    cursor = stopped[-1].id
    assert cursor is not None
    await replicas.survivor.open_session(SANDBOX, SESSION, spec)
    async with aclosing(replicas.survivor.events(SANDBOX, SESSION, after_sequence=cursor)) as frames:
        lines = frame_lines(frames)
        assert (await next_message(lines)).event == "attached"
        await replicas.survivor.send(SANDBOX, SESSION, pb.Input(input_id="resumed-input", text="RESUMED_REPLICA"))
        model.reply(await model.request(), Text("RESUMED_REPLICA"))
        async with asyncio.timeout(10):
            resumed = await read_until(lines, "turnCompleted")
        assert all(message.event == "event" for message in resumed)
        assert any(message.data.get("harnessStarted", {}).get("resumed") for message in resumed)
        assert [message.id for message in resumed] == list(range(cursor + 1, cursor + len(resumed) + 1))
        await replicas.survivor.shutdown(SANDBOX, SESSION)
    model.assert_quiescent()


async def test_stored_conversation_stream_does_not_require_reachable_runner(
    replicas: Replicas, store: TrajectoryStore, spec: pb.SessionSpec
) -> None:
    await replicas.owner.open_session(SANDBOX, SESSION, spec)
    await replicas.survivor.shutdown(SANDBOX, SESSION)
    async with aclosing(replicas.survivor.events(SANDBOX, SESSION, after_sequence=0)) as frames:
        lines = frame_lines(frames)
        await next_message(lines)
        async with asyncio.timeout(10):
            stored = await read_until(lines, "harnessExited")
            assert (await next_message(lines)).event == "end"
    await replicas.owner.close()
    await replicas.survivor.close()

    async def unavailable(name: str) -> str:
        raise ConnectionError(f"test runner {name} is unavailable")

    offline = RunnerBridge(address_of=unavailable, store=store)
    try:
        async with asyncio.timeout(10), aclosing(offline.events(SANDBOX, SESSION, after_sequence=0)) as frames:
            lines = frame_lines(frames)
            assert (await next_message(lines)).event == "attached"
            assert await read_until(lines, "harnessExited") == stored
            assert (await next_message(lines)).event == "end"
    finally:
        await offline.close()


if __name__ == "__main__":
    pytest_bazel.main()
