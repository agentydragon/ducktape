"""One browser-shaped script over the bridge against a local runner, run for both harnesses: open a
session, stream it, send an input while streaming, open a second tab on the same session, reconnect
from the last event id, shut down; and the thread the store kept of all of it."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import shutil
import signal
from collections.abc import AsyncIterator, Callable
from contextlib import aclosing
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest
import pytest_bazel
from google.protobuf.json_format import MessageToDict
from google.protobuf.timestamp_pb2 import Timestamp
from sqlalchemy import select
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_delay, wait_fixed

from agentplane.app.action_policy import ActionPolicyInventory
from agentplane.app.api import create_app
from agentplane.app.changes import Changes
from agentplane.app.conftest import _CALL_REPORT, AGENT_AUTH
from agentplane.app.database import connect
from agentplane.app.database_updates import Channel, DatabaseUpdates
from agentplane.app.decisions import DecisionsClient
from agentplane.app.egress_access import EgressAccess
from agentplane.app.identity import TokenReviewer
from agentplane.app.live import LiveIndex
from agentplane.app.model_catalog import ModelCatalog, ModelOption
from agentplane.app.operator_sessions import OperatorSessionStore
from agentplane.app.testing.model_test_data import TEST_REASONING_EFFORTS
from agentplane.app.threads.bridge import RunnerAdmissionTimeoutError, RunnerBridge
from agentplane.app.threads.events.event_log import EventLogStore, FeedError
from agentplane.app.threads.events.stream import follow
from agentplane.app.threads.ingestion import Feed, Ingester, Ingestion
from agentplane.app.threads.models import ThreadCheckpoint, ThreadEntity, ThreadPayloadChunk
from agentplane.app.threads.sessions import SandboxSessions
from agentplane.app.threads.store import ThreadStore
from agentplane.app.threads.view.content import ContentStore
from agentplane.app.threads.view.views import ThreadOperationalState
from agentplane.protocol import command_pb2, event_log_pb2, event_pb2
from agentplane.runner import protocol_pb2, service
from agentplane.runner.client import RunnerClient
from agentplane.runner.errors import RunnerError, StreamClosedError
from agentplane.runner.harness import Harness
from agentplane.runner.session import Session
from agentplane.runner.testing.fixtures import RunnerClientFactory, RunnerHandle
from agentplane.runner.testing.scripted_model import ScriptedModel, ShellCall, Text
from agentplane.sandbox_service.client import Attachment, SandboxServiceClient
from agentplane.sandbox_service.testing.backend import Endpoint, seed_runner
from agentplane.sandbox_service.testing.fake_inventory import FakeCoreV1Api, FakeCustomObjectsApi
from util.net import bind_free_port
from util.testing.asgi import serve_app_in_loop
from util.testing.undeclared_outputs import undeclared_outputs_dir

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf

SANDBOX = "bridge-test-sandbox"
SESSION = "bridge-1"
SESSIONS = f"/sandboxes/{SANDBOX}/sessions"


@pytest.fixture
async def failed_native_journal(
    request: pytest.FixtureRequest, runner: RunnerHandle, caplog: pytest.LogCaptureFixture
) -> AsyncIterator[None]:
    """Preserve native history and journal evidence when an app-level bridge case fails."""
    caplog.set_level(logging.INFO, logger="httpx")
    yield
    report = request.node.stash.get(_CALL_REPORT, None)
    if report is None or not report.failed:
        return
    for session_id, session in runner.runner.sessions.items():
        target = undeclared_outputs_dir() / request.node.name / session_id
        for dialect, history in (("claude", "projects"), ("codex", "sessions")):
            source = session.directory / dialect / history
            if source.exists():
                shutil.copytree(source, target / dialect / history, dirs_exist_ok=True)
    sessions: dict[str, list[dict[str, Any]]] = {}
    for session_id, session in runner.runner.sessions.items():
        entries = await session.journal.since(0, limit=512)
        sessions[session_id] = [MessageToDict(entry, preserving_proto_field_name=True) for entry in entries]
    (undeclared_outputs_dir() / f"{request.node.name}-native-journal.json").write_text(
        json.dumps(sessions, indent=2, sort_keys=True)
    )


async def _thread_id(http: httpx.AsyncClient, session_id: str = SESSION) -> str:
    rows = (await http.get("/threads", params={"sandbox": SANDBOX, "session_id": session_id})).json()
    assert len(rows) == 1
    return str(rows[0]["id"])


def _commands(thread_id: str) -> str:
    return f"/threads/{thread_id}/commands"


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
    """Entries up to and including the first whose Event payload carries `key`."""
    seen: list[SseMessage] = []
    while True:
        message = await next_message(lines)
        seen.append(message)
        if key in message.data.get("event", {}):
            return seen


@pytest.fixture
def sandbox_runner_port(runner: RunnerHandle) -> int:
    return runner.port


@pytest.fixture
async def local_runners(
    live_index: LiveIndex, sandbox_endpoint: Endpoint, custom_objects: FakeCustomObjectsApi, core_v1: FakeCoreV1Api
) -> AsyncIterator[SandboxSessions]:
    """`SANDBOX` running, its Pod at the local runner's address."""
    live_index.sandboxes[SANDBOX], live_index.pods[SANDBOX] = seed_runner(custom_objects, core_v1, SANDBOX)
    runners = SandboxSessions(live_index, sandbox_endpoint.client())
    yield runners
    await runners.close()


@pytest.fixture
async def app_url(
    local_runners: SandboxSessions,
    inventory: SandboxServiceClient,
    store: ThreadStore,
    database_updates: DatabaseUpdates,
    operator_sessions: OperatorSessionStore,
    egress: EgressAccess,
    decisions: DecisionsClient,
    live_index: LiveIndex,
    action_policy: ActionPolicyInventory,
    reviewer: TokenReviewer,
    event_logs: EventLogStore,
    content: ContentStore,
    ingestion: Ingestion,
) -> AsyncIterator[str]:
    """The app served by uvicorn, with the one test sandbox resolving to the local runner. The
    server is real because SSE needs a response that streams, which an in-process ASGI transport
    would buffer."""
    ingester = Ingester(runners=local_runners, event_logs=event_logs, ingestion=ingestion)
    bridge = RunnerBridge(runners=local_runners, event_logs=event_logs, content=content, ingester=ingester)
    app = create_app(
        inventory,
        bridge,
        store,
        ModelCatalog(
            models=[
                ModelOption(
                    model="bridge-model", display_name="Bridge Model", reasoning_efforts=list(TEST_REASONING_EFFORTS)
                )
            ],
            harnesses={harness: ["bridge-model"] for harness in Harness},
        ),
        egress,
        decisions,
        live_index,
        action_policy,
        reviewer=reviewer,
        event_logs=event_logs,
        content=content,
        database_updates=database_updates,
        operator_sessions=operator_sessions,
    )
    sock = bind_free_port()
    try:
        async with serve_app_in_loop(app, sock=sock):
            yield f"http://127.0.0.1:{sock.getsockname()[1]}"
    finally:
        await ingester.close()


async def test_the_bridge_streams_a_turn_to_every_tab_and_resumes_from_the_last_event_id(
    app_url: str, model: ScriptedModel, spec: protocol_pb2.SessionSpec, failed_native_journal: None
) -> None:
    async with httpx.AsyncClient(base_url=app_url, timeout=60, headers=AGENT_AUTH) as http:
        opened = await http.post(SESSIONS, json={"session_id": SESSION, "spec": MessageToDict(spec)})
        assert opened.status_code == 201, opened.text
        assert opened.json()["harnessState"] == "HARNESS_STATE_RUNNING"
        assert [row["sessionId"] for row in (await http.get(SESSIONS)).json()] == [SESSION]
        thread_id = await _thread_id(http)

        async with http.stream("GET", f"/threads/{thread_id}/events/stream") as first_tab:
            first = first_tab.aiter_lines()
            assert (await next_message(first)).event == "attached"
            reopened = await http.post(SESSIONS, json={"session_id": SESSION, "spec": MessageToDict(spec)})
            assert reopened.status_code == 201, reopened.text
            accepted = await http.post(
                _commands(thread_id),
                json={"commandId": "input-1", "submitInput": {"text": "Reply with exactly: BRIDGE_OK"}},
            )
            assert accepted.status_code == 200, accepted.text
            assert accepted.json()["event"]["commandAdmitted"]["command"] == {
                "commandId": "input-1",
                "submitInput": {"text": "Reply with exactly: BRIDGE_OK"},
            }
            retry = await http.post(
                _commands(thread_id),
                json={"commandId": "input-1", "submitInput": {"text": "Reply with exactly: BRIDGE_OK"}},
            )
            assert retry.status_code == 200, retry.text
            assert retry.json() == accepted.json()
            request = await model.request()
            assert request.user_texts[-1] == "Reply with exactly: BRIDGE_OK"
            await model.reply(request, Text("BRIDGE_OK"))
            seen = await read_until(first, "turnCompleted")
            # Every runner event, in order, from the start of the session's log: replay and live alike.
            assert [message.id for message in seen] == list(range(1, len(seen) + 1))
            assert all(message.event == "event" for message in seen)
            assert seen[-1].data["event"]["turnCompleted"]["status"] == "TURN_STATUS_COMPLETED"
            assert all(message.data["origin"]["sourceId"] for message in seen)
            assert any("native" in message.data["event"] for message in seen)
            completed = [
                message.data["event"]["itemCompleted"] for message in seen if "itemCompleted" in message.data["event"]
            ]
            assert [item["text"] for item in completed] == ["BRIDGE_OK"]

            # A second tab loads the first tab's history, then both follow the same committed log.
            async with http.stream("GET", f"/threads/{thread_id}/events/stream") as second_tab:
                second = second_tab.aiter_lines()
                assert (await next_message(second)).event == "attached"
                assert await read_until(second, "turnCompleted") == seen
                accepted = await http.post(
                    _commands(thread_id),
                    json={"commandId": "input-2", "submitInput": {"text": "Reply with exactly: BRIDGE_TWO"}},
                )
                assert accepted.status_code == 200, accepted.text
                assert accepted.json()["event"]["commandAdmitted"]["command"] == {
                    "commandId": "input-2",
                    "submitInput": {"text": "Reply with exactly: BRIDGE_TWO"},
                }
                request = await model.request()
                assert request.user_texts[-1] == "Reply with exactly: BRIDGE_TWO"
                await model.reply(request, Text("BRIDGE_TWO"))
                on_first, on_second = await asyncio.gather(
                    read_until(first, "turnCompleted"), read_until(second, "turnCompleted")
                )
                assert on_first == on_second
                assert on_first[0].id == len(seen) + 1
                assert [
                    message.data["event"]["itemCompleted"]["text"]
                    for message in on_first
                    if "itemCompleted" in message.data["event"]
                ] == ["BRIDGE_TWO"]

        # A browser reconnecting sends the last id it saw; the next event follows it without a gap.
        cut = seen[len(seen) // 2].id
        assert cut is not None
        async with http.stream(
            "GET", f"/threads/{thread_id}/events/stream", headers={"Last-Event-ID": str(cut)}
        ) as stream:
            lines = stream.aiter_lines()
            assert (await next_message(lines)).event == "attached"
            assert (await next_message(lines)).id == cut + 1

        stopped = await http.post(_commands(thread_id), json={"commandId": "stop-bridge", "stopRunnerSession": {}})
        assert stopped.status_code == 200, stopped.text
        assert stopped.json()["event"]["commandAdmitted"]["command"] == {
            "commandId": "stop-bridge",
            "stopRunnerSession": {},
        }

        # The store kept the whole thread, readable without the runner: both turns, the raw
        # frames, and the exit the shutdown caused.
        (thread,) = (await http.get("/threads")).json()
        assert thread["id"] == thread_id
        assert (thread["sandbox"], thread["session_id"], thread["model"]) == (SANDBOX, SESSION, spec.model)
        stored = await _stored_events(http, thread["id"], until="harnessExited")
        assert [entry["cursor"] for entry in stored] == [str(n) for n in range(1, len(stored) + 1)]
        assert [entry["event"]["itemCompleted"]["text"] for entry in stored if "itemCompleted" in entry["event"]] == [
            "BRIDGE_OK",
            "BRIDGE_TWO",
        ]
        assert any("native" in entry["event"] for entry in stored)
        assert (await http.get(f"/threads/{thread['id']}")).json()["last_cursor"] == len(stored)
        (summary,) = (await http.get(SESSIONS)).json()
        assert summary["harnessState"] == "HARNESS_STATE_STOPPED"

        resumed = await http.post(f"/threads/{thread_id}/resume")
        assert resumed.status_code == 200, resumed.text
        assert resumed.json()["sessionId"] == SESSION
        assert resumed.json()["spec"]["harness"] == protocol_pb2.Harness.Name(spec.harness)
        assert resumed.json()["harnessState"] == "HARNESS_STATE_RUNNING"
        assert (await http.get("/threads")).json()[0]["id"] == thread_id

        # The Thread route resumes the session from the runner-owned stored spec. The event stream
        # continues after the previous shutdown and the native request still has both completed
        # turns in context.
        previous_cursor = int(stored[-1]["cursor"])
        async with http.stream(
            "GET", f"/threads/{thread_id}/events/stream", headers={"Last-Event-ID": str(previous_cursor)}
        ) as resumed_stream:
            resumed_lines = resumed_stream.aiter_lines()
            await next_message(resumed_lines)
            started = await read_until(resumed_lines, "harnessStarted")
            assert started[-1].data["event"]["harnessStarted"]["resumed"] is True
            accepted = await http.post(
                _commands(thread_id),
                json={"commandId": "input-after-resume", "submitInput": {"text": "Reply with exactly: BRIDGE_RESUMED"}},
            )
            assert accepted.status_code == 200, accepted.text
            request = await model.request()
            assert request.user_texts == [
                "Reply with exactly: BRIDGE_OK",
                "Reply with exactly: BRIDGE_TWO",
                "Reply with exactly: BRIDGE_RESUMED",
            ]
            assert request.assistant_texts == ["BRIDGE_OK", "BRIDGE_TWO"]
            await model.reply(request, Text("BRIDGE_RESUMED"))
            continued = await read_until(resumed_lines, "turnCompleted")
            assert [message.id for message in [*started, *continued]] == list(
                range(previous_cursor + 1, previous_cursor + len(started) + len(continued) + 1)
            )
            assert continued[-1].data["event"]["turnCompleted"]["status"] == "TURN_STATUS_COMPLETED"


async def _stored_events(
    http: httpx.AsyncClient,
    thread_id: str,
    *,
    until: str,
    matches: Callable[[list[dict[str, Any]]], bool] | None = None,
) -> list[dict[str, Any]]:
    """The thread's stored entries once one carrying `until` has landed; the feed writes them as
    they arrive, a moment after the runner emitted them."""
    async for attempt in AsyncRetrying(
        stop=stop_after_delay(30), wait=wait_fixed(0.2), retry=retry_if_exception_type(AssertionError)
    ):
        with attempt:
            response = await http.get(f"/threads/{thread_id}/events")
            assert response.status_code == 200, response.text
            entries: list[dict[str, Any]] = response.json()
            assert any(until in entry["event"] for entry in entries), f"no {until} stored yet"
            assert matches is None or matches(entries), f"stored events have not reached the expected state: {entries=}"
    return entries


async def _folded_items(store: ThreadStore, thread_id: str) -> tuple[dict[str, ThreadEntity], list[ThreadPayloadChunk]]:
    """The current item rows and payload chunks from the app's materialized thread fold."""
    async with store._sessions() as session:
        checkpoint = await session.get(ThreadCheckpoint, UUID(thread_id))
        if checkpoint is None:
            return {}, []
        rows = await session.scalars(
            select(ThreadEntity).where(
                ThreadEntity.thread_id == UUID(thread_id),
                ThreadEntity.projection_epoch == checkpoint.projection_epoch,
                ThreadEntity.entity_kind == "item",
            )
        )
        chunks = await session.scalars(
            select(ThreadPayloadChunk).where(
                ThreadPayloadChunk.thread_id == UUID(thread_id),
                ThreadPayloadChunk.projection_epoch == checkpoint.projection_epoch,
            )
        )
        return {row.entity_id: row for row in rows}, list(chunks)


def _folded_body(chunks: list[ThreadPayloadChunk], reference: dict[str, Any] | None) -> str:
    if reference is None:
        return ""
    indexed = {
        chunk.chunk_index: chunk.text
        for chunk in chunks
        if chunk.projection_epoch == reference["projection_epoch"]
        and chunk.owner_cursor == int(reference["owner_cursor"])
        and chunk.owner_id == reference["owner_id"]
        and chunk.field == reference["field"]
        and str(chunk.generation) == str(reference["generation"])
    }
    return "".join(indexed[index] for index in range(int(reference["chunk_count"])) if index in indexed)


async def _wait_for_folded_items(
    store: ThreadStore, thread_id: str, matches: Callable[[dict[str, ThreadEntity], list[ThreadPayloadChunk]], bool]
) -> tuple[dict[str, ThreadEntity], list[ThreadPayloadChunk]]:
    async for attempt in AsyncRetrying(
        stop=stop_after_delay(10), wait=wait_fixed(0.2), retry=retry_if_exception_type(AssertionError)
    ):
        with attempt:
            items, chunks = await _folded_items(store, thread_id)
            details = {
                item_id: {
                    "state": item.state,
                    "text": _folded_body(chunks, item.text_ref),
                    "arguments": _folded_body(chunks, item.arguments_ref),
                    "output": _folded_body(chunks, item.output_ref),
                }
                for item_id, item in items.items()
            }
            assert matches(items, chunks), f"folded items did not reach the expected state: {details=}"
            return items, chunks
    raise AssertionError("unreachable")


async def _next_model_request(model: ScriptedModel) -> Any:
    async with asyncio.timeout(30):
        return await model.request()


async def _open_thread_with_seed(
    http: httpx.AsyncClient,
    model: ScriptedModel,
    spec: protocol_pb2.SessionSpec,
    *,
    session_id: str,
    seed_prompt: str,
    seed_answer: str,
) -> str:
    opened = await http.post(
        f"/sandboxes/{SANDBOX}/sessions", json={"session_id": session_id, "spec": MessageToDict(spec)}
    )
    assert opened.status_code == 201, opened.text
    thread_id = await _thread_id(http, session_id)
    accepted = await http.post(
        _commands(thread_id), json={"commandId": "seed-input", "submitInput": {"text": seed_prompt}}
    )
    assert accepted.status_code == 200, accepted.text
    request = await _next_model_request(model)
    assert request.user_texts[-1] == seed_prompt
    await model.reply(request, Text(seed_answer))
    await _stored_events(http, thread_id, until="turnCompleted")
    return thread_id


def _has_turn_status(entries: list[dict[str, Any]], status: str) -> bool:
    return any(entry["event"].get("turnCompleted", {}).get("status") == status for entry in entries)


async def test_http_error_is_archived_as_failed_turn_and_follow_up_succeeds(
    app_url: str, model: ScriptedModel, spec: protocol_pb2.SessionSpec, failed_native_journal: None
) -> None:
    """A failed model turn stays admitted and replayable; a later input is a separate turn."""
    async with httpx.AsyncClient(base_url=app_url, timeout=60, headers=AGENT_AUTH) as http:
        opened = await http.post(SESSIONS, json={"session_id": SESSION, "spec": MessageToDict(spec)})
        assert opened.status_code == 201, opened.text
        thread_id = await _thread_id(http)
        accepted = await http.post(
            _commands(thread_id),
            json={"commandId": "http-failure", "submitInput": {"text": "Reply with exactly: HTTP_ERROR"}},
        )
        assert accepted.status_code == 200, accepted.text
        for _ in range(3):
            request = await _next_model_request(model)
            assert request.user_texts[-1] == "Reply with exactly: HTTP_ERROR"
            await model.http_error(request)
        failed = await _stored_events(
            http,
            thread_id,
            until="turnCompleted",
            matches=lambda entries: _has_turn_status(entries, "TURN_STATUS_FAILED"),
        )
        assert [
            entry["event"]["commandAdmitted"]["command"]["commandId"]
            for entry in failed
            if "commandAdmitted" in entry["event"]
        ] == ["http-failure"]
        assert any("native" in entry["event"] for entry in failed)
        assert (await http.get(f"/threads/{thread_id}")).json()["last_turn_status"] == "TURN_STATUS_FAILED"
        # Re-reading the archive, as on a UI reload, retains the same terminal evidence.
        assert (await http.get(f"/threads/{thread_id}/events")).json() == failed
        accepted = await http.post(
            _commands(thread_id),
            json={"commandId": "http-follow-up", "submitInput": {"text": "Reply with exactly: AFTER_HTTP_ERROR_OK"}},
        )
        assert accepted.status_code == 200, accepted.text
        request = await _next_model_request(model)
        assert request.user_texts[-1] == "Reply with exactly: AFTER_HTTP_ERROR_OK"
        await model.reply(request, Text("AFTER_HTTP_ERROR_OK"))
        recovered = await _stored_events(
            http,
            thread_id,
            until="turnCompleted",
            matches=lambda entries: (
                _has_turn_status(entries, "TURN_STATUS_COMPLETED")
                and sum("turnCompleted" in entry["event"] for entry in entries) == 2
            ),
        )
        assert [
            entry["event"]["commandAdmitted"]["command"]["commandId"]
            for entry in recovered
            if "commandAdmitted" in entry["event"]
        ] == ["http-failure", "http-follow-up"]
        assert (await http.get(f"/threads/{thread_id}")).json()["last_turn_status"] == "TURN_STATUS_COMPLETED"


@pytest.mark.parametrize("stop_mode", ["interrupt", "shutdown", "kill"])
async def test_stream_recovery_reports_the_content_the_model_receives(
    app_url: str,
    model: ScriptedModel,
    runner: RunnerHandle,
    store: ThreadStore,
    spec: protocol_pb2.SessionSpec,
    failed_native_journal: None,
    stop_mode: str,
) -> None:
    seed_prompt, seed_answer = "Remember the seed prompt", "SEED_ANSWER_FOR_STREAM_RECOVERY"
    partial = "PARTIAL_STREAM_MUST_STAY_UNFINISHED"
    async with httpx.AsyncClient(base_url=app_url, timeout=60, headers=AGENT_AUTH) as http:
        thread_id = await _open_thread_with_seed(
            http, model, spec, session_id=SESSION, seed_prompt=seed_prompt, seed_answer=seed_answer
        )
        accepted = await http.post(
            _commands(thread_id), json={"commandId": "streaming-input", "submitInput": {"text": "Start a long answer"}}
        )
        assert accepted.status_code == 200, accepted.text
        request = await _next_model_request(model)
        await model.hold_after_first_delta(request, Text(partial))
        items, chunks = await _wait_for_folded_items(
            store,
            thread_id,
            lambda rows, payloads: any(_folded_body(payloads, row.text_ref) == partial for row in rows.values()),
        )
        item_id = next(row.entity_id for row in items.values() if _folded_body(chunks, row.text_ref) == partial)
        assert items[item_id].state["completion"] is None
        recovery_cursor = await _interrupt_and_recover(http, runner, thread_id, stop_mode)
        items, chunks = await _wait_for_folded_items(
            store,
            thread_id,
            lambda rows, payloads: (
                rows[item_id].revision_cursor >= recovery_cursor
                and rows[item_id].state["recovery"]
                in {event_pb2.RECOVERY_DISPOSITION_RETAINED, event_pb2.RECOVERY_DISPOSITION_ABSENT}
            ),
        )
        assert _folded_body(chunks, items[item_id].text_ref) == partial
        disposition = items[item_id].state["recovery"]
        accepted = await http.post(
            _commands(thread_id),
            json={"commandId": "stream-recovery-input", "submitInput": {"text": "Continue after recovery"}},
        )
        assert accepted.status_code == 200, accepted.text
        request = await _next_model_request(model)
        assert seed_prompt in request.user_texts
        assert seed_answer in request.assistant_texts
        assert (partial in request.assistant_texts) == (disposition == event_pb2.RECOVERY_DISPOSITION_RETAINED)
        await model.reply(request, Text("STREAM_RECOVERY_DONE"))
        await _stored_events(
            http,
            thread_id,
            until="turnCompleted",
            matches=lambda entries: any(
                entry["event"].get("itemCompleted", {}).get("text") == "STREAM_RECOVERY_DONE" for entry in entries
            ),
        )


async def _interrupt_and_recover(http: httpx.AsyncClient, runner: RunnerHandle, thread_id: str, mode: str) -> int:
    session = runner.runner.sessions[SESSION]
    process = session.process
    assert process is not None
    turn_id = session.active_turn_id
    before = session.journal.last_cursor
    if mode == "kill":
        os.killpg(process.native_pid, signal.SIGKILL)
    else:
        operation = {"interruptTurn": {"turnId": turn_id}} if mode == "interrupt" else {"stopRunnerSession": {}}
        response = await http.post(_commands(thread_id), json={"commandId": "interrupt-for-recovery", **operation})
        assert response.status_code == 200, response.text
    expected = "TURN_STATUS_PROCESS_LOST" if mode == "kill" else "TURN_STATUS_INTERRUPTED"
    await _stored_events(
        http,
        thread_id,
        until="turnCompleted",
        matches=lambda entries: any(
            entry["event"].get("turnCompleted", {}).get("turnId") == turn_id
            and entry["event"]["turnCompleted"]["status"] == expected
            for entry in entries
        ),
    )
    if mode != "interrupt":
        await _stored_events(http, thread_id, until="harnessExited")
        resumed = await http.post(f"/threads/{thread_id}/resume")
        assert resumed.status_code == 200, resumed.text
        assert resumed.json()["sessionId"] == SESSION
    else:
        assert session.process is process
        assert process.running
    await _stored_events(
        http,
        thread_id,
        until="conversationReconciled",
        matches=lambda entries: any(
            entry["event"].get("conversationReconciled", {}).get("turnId") == turn_id for entry in entries
        ),
    )
    reports = [
        entry
        for entry in await session.journal.since(before, limit=512)
        if entry.event.HasField("conversation_reconciled") and entry.event.conversation_reconciled.turn_id == turn_id
    ]
    assert reports
    expected_report = reports[-1].cursor
    entries = await _stored_events(
        http,
        thread_id,
        until="conversationReconciled",
        matches=lambda entries: any(
            int(entry["origin"]["sequence"]) == expected_report
            and entry["event"].get("conversationReconciled", {}).get("turnId") == turn_id
            for entry in entries
        ),
    )

    return next(
        int(entry["cursor"])
        for entry in entries
        if int(entry["origin"]["sequence"]) == expected_report
        and entry["event"].get("conversationReconciled", {}).get("turnId") == turn_id
    )


@pytest.mark.parametrize("stop_mode", ["interrupt", "shutdown", "kill"])
async def test_tool_recovery_reports_context_without_repeating_side_effects(
    app_url: str,
    model: ScriptedModel,
    runner: RunnerHandle,
    store: ThreadStore,
    spec: protocol_pb2.SessionSpec,
    workspace: Path,
    failed_native_journal: None,
    stop_mode: str,
) -> None:
    seed_prompt, seed_answer = "Remember the tool test seed", "SEED_ANSWER_FOR_TOOL_RECOVERY"
    recovery_prompt = "After resume, answer without rerunning the old tool"
    marker = workspace / "tool-side-effects"
    ready = workspace / "tool-started"
    token = "UNKNOWN_TOOL_SIDE_EFFECT"
    command = (
        f"printf '%s\\n' {shlex.quote(token)} >> {shlex.quote(str(marker))}; "
        f"printf '%s\\n' ready > {shlex.quote(str(ready))}; "
        "printf 'partial tool output'; exec sleep 3600"
    )
    async with httpx.AsyncClient(base_url=app_url, timeout=60, headers=AGENT_AUTH) as http:
        thread_id = await _open_thread_with_seed(
            http, model, spec, session_id=SESSION, seed_prompt=seed_prompt, seed_answer=seed_answer
        )
        accepted = await http.post(
            _commands(thread_id),
            json={"commandId": "tool-input", "submitInput": {"text": "Run the blocking side effect once"}},
        )
        assert accepted.status_code == 200, accepted.text
        request = await _next_model_request(model)
        await model.reply(request, ShellCall("inflight-tool", command))
        async for attempt in AsyncRetrying(
            stop=stop_after_delay(30), wait=wait_fixed(0.1), retry=retry_if_exception_type(AssertionError)
        ):
            with attempt:
                assert ready.exists(), "the shell tool did not reach its controlled blocking point"
        assert marker.read_text().splitlines() == [token]
        items, chunks = await _wait_for_folded_items(
            store,
            thread_id,
            lambda rows, payloads: any(
                row.state["completion"] is None and token in _folded_body(payloads, row.arguments_ref)
                for row in rows.values()
            ),
        )
        incomplete_tools = [
            row
            for row in items.values()
            if row.state["completion"] is None and token in _folded_body(chunks, row.arguments_ref)
        ]
        assert len(incomplete_tools) == 1
        assert _folded_body(chunks, incomplete_tools[0].output_ref) == ""

        item_id = incomplete_tools[0].entity_id
        recovery_cursor = await _interrupt_and_recover(http, runner, thread_id, stop_mode)
        items, chunks = await _wait_for_folded_items(
            store,
            thread_id,
            lambda rows, payloads: (
                rows[item_id].revision_cursor >= recovery_cursor
                and rows[item_id].state["recovery"]
                in {
                    event_pb2.RECOVERY_DISPOSITION_RETAINED,
                    event_pb2.RECOVERY_DISPOSITION_ABSENT,
                    event_pb2.RECOVERY_DISPOSITION_REVISED,
                }
            ),
        )
        recovered = items[item_id]
        assert recovered.state["tool_succeeded"] is not True
        if stop_mode == "kill":
            assert recovered.state["completion"] is None
        assert marker.read_text().splitlines() == [token]
        accepted = await http.post(
            _commands(thread_id), json={"commandId": "tool-recovery-input", "submitInput": {"text": recovery_prompt}}
        )
        assert accepted.status_code == 200, accepted.text
        request = await _next_model_request(model)
        assert seed_prompt in request.user_texts
        assert seed_answer in request.assistant_texts
        assert ("inflight-tool" in request.tool_calls) == (
            recovered.state["recovery"] != event_pb2.RECOVERY_DISPOSITION_ABSENT
        )
        recovered_output = _folded_body(chunks, recovered.output_ref)
        if recovered_output and recovered.state["recovery"] != event_pb2.RECOVERY_DISPOSITION_ABSENT:
            assert any(recovered_output in output.text for output in request.tool_outputs)
        assert marker.read_text().splitlines() == [token]
        await model.reply(request, Text("TOOL_RECOVERY_DONE"))
        await _stored_events(
            http,
            thread_id,
            until="turnCompleted",
            matches=lambda entries: _has_turn_status(entries, "TURN_STATUS_COMPLETED"),
        )
        assert marker.read_text().splitlines() == [token]


@pytest.mark.parametrize("stop_mode", ["interrupt", "shutdown", "kill"])
async def test_resume_after_tool_completion_preserves_the_result_and_does_not_repeat_it(
    app_url: str,
    model: ScriptedModel,
    runner: RunnerHandle,
    store: ThreadStore,
    spec: protocol_pb2.SessionSpec,
    workspace: Path,
    failed_native_journal: None,
    stop_mode: str,
) -> None:
    seed_prompt, seed_answer = "Remember the completed tool seed", "SEED_ANSWER_FOR_COMPLETED_TOOL"
    recovery_prompt = "Continue after the earlier tool and interrupted answer"
    marker = workspace / "completed-tool-side-effects"
    token = "COMPLETED_TOOL_SIDE_EFFECT"
    tool_output = "COMPLETED_TOOL_OUTPUT"
    command = (
        f"printf '%s\\n' {shlex.quote(token)} >> {shlex.quote(str(marker))}; printf '%s' {shlex.quote(tool_output)}"
    )
    partial = "PARTIAL_AFTER_COMPLETED_TOOL"
    async with httpx.AsyncClient(base_url=app_url, timeout=60, headers=AGENT_AUTH) as http:
        thread_id = await _open_thread_with_seed(
            http, model, spec, session_id=SESSION, seed_prompt=seed_prompt, seed_answer=seed_answer
        )
        accepted = await http.post(
            _commands(thread_id),
            json={"commandId": "completed-tool-input", "submitInput": {"text": "Run one completed tool"}},
        )
        assert accepted.status_code == 200, accepted.text
        request = await _next_model_request(model)
        await model.reply(request, ShellCall("completed-tool", command))
        continuation = await _next_model_request(model)
        assert any(tool_output in result.text for result in continuation.tool_outputs)
        assert marker.read_text().splitlines() == [token]
        await model.hold_after_first_delta(continuation, Text(partial))
        await _stored_events(
            http,
            thread_id,
            until="textDelta",
            matches=lambda entries: any(
                entry["event"].get("textDelta", {}).get("text") == partial for entry in entries
            ),
        )
        await _wait_for_folded_items(
            store,
            thread_id,
            lambda rows, payloads: (
                any(
                    row.state["completion"] == "tool"
                    and row.state["tool_succeeded"] is True
                    and tool_output in _folded_body(payloads, row.output_ref)
                    for row in rows.values()
                )
                and any(
                    row.state["completion"] is None and _folded_body(payloads, row.text_ref) == partial
                    for row in rows.values()
                )
            ),
        )

        recovery_cursor = await _interrupt_and_recover(http, runner, thread_id, stop_mode)
        items, chunks = await _folded_items(store, thread_id)
        completed_tools = [
            row
            for row in items.values()
            if row.state["completion"] == "tool" and tool_output in _folded_body(chunks, row.output_ref)
        ]
        assert len(completed_tools) == 1
        assert completed_tools[0].state["tool_succeeded"] is True
        accepted = await http.post(
            _commands(thread_id),
            json={"commandId": "completed-tool-recovery-input", "submitInput": {"text": recovery_prompt}},
        )
        assert accepted.status_code == 200, accepted.text
        request = await _next_model_request(model)
        async with request._exchange as exchange:
            assert seed_prompt in request.user_texts
            assert seed_answer in request.assistant_texts
            items, chunks = await _wait_for_folded_items(
                store,
                thread_id,
                lambda rows, payloads: (
                    rows[completed_tools[0].entity_id].revision_cursor >= recovery_cursor
                    and rows[completed_tools[0].entity_id].state["recovery"]
                    in {event_pb2.RECOVERY_DISPOSITION_RETAINED, event_pb2.RECOVERY_DISPOSITION_ABSENT}
                ),
            )
            recovered_tool = items[completed_tools[0].entity_id]
            assert recovered_tool.state["tool_succeeded"] is True
            assert tool_output in _folded_body(chunks, recovered_tool.output_ref)
            assert any(tool_output in result.text for result in request.tool_outputs) == (
                recovered_tool.state["recovery"] == event_pb2.RECOVERY_DISPOSITION_RETAINED
            )
            assert marker.read_text().splitlines() == [token]
            await exchange.send(*model.stream([Text("COMPLETED_TOOL_RECOVERY_DONE")]))
        await _stored_events(
            http,
            thread_id,
            until="turnCompleted",
            matches=lambda entries: _has_turn_status(entries, "TURN_STATUS_COMPLETED"),
        )
        assert marker.read_text().splitlines() == [token]


async def test_thread_resume_reports_missing_runner_recovery_state(
    app_url: str, event_logs: EventLogStore, spec: protocol_pb2.SessionSpec
) -> None:
    thread_id = await event_logs.open(SANDBOX, "missing-runner-session", spec)
    async with httpx.AsyncClient(base_url=app_url, timeout=60, headers=AGENT_AUTH) as http:
        response = await http.post(f"/threads/{thread_id}/resume")
    assert response.status_code == 409


async def test_the_feed_records_a_turn_nobody_is_watching(
    app_url: str, model: ScriptedModel, spec: protocol_pb2.SessionSpec
) -> None:
    """Opening a session starts its feed, so a turn driven over REST alone lands in the store."""
    async with httpx.AsyncClient(base_url=app_url, timeout=60, headers=AGENT_AUTH) as http:
        opened = await http.post(SESSIONS, json={"session_id": "unwatched", "spec": MessageToDict(spec)})
        assert opened.status_code == 201, opened.text
        thread_id = await _thread_id(http, "unwatched")
        accepted = await http.post(
            _commands(thread_id),
            json={"commandId": "input-1", "submitInput": {"text": "Reply with exactly: UNWATCHED_OK"}},
        )
        assert accepted.status_code == 200, accepted.text
        request = await model.request()
        assert request.user_texts[-1] == "Reply with exactly: UNWATCHED_OK"
        await model.reply(request, Text("UNWATCHED_OK"))
        (thread,) = (await http.get("/threads")).json()
        stored = await _stored_events(http, thread["id"], until="turnCompleted")
        assert [entry["event"]["itemCompleted"]["text"] for entry in stored if "itemCompleted" in entry["event"]] == [
            "UNWATCHED_OK"
        ]
        assert (
            await http.post(_commands(thread_id), json={"commandId": "stop-unwatched", "stopRunnerSession": {}})
        ).status_code == 200
        assert (await http.get("/threads/00000000-0000-0000-0000-000000000000/events")).status_code == 404


async def test_the_bridge_reports_what_the_runner_refuses(app_url: str) -> None:
    async with httpx.AsyncClient(base_url=app_url, timeout=60, headers=AGENT_AUTH) as http:
        unknown = await http.post(
            "/threads/00000000-0000-0000-0000-000000000000/commands",
            json={"commandId": "x", "submitInput": {"text": "hello"}},
        )
        assert unknown.status_code == 404
        malformed = await http.post("/threads/00000000-0000-0000-0000-000000000000/commands", json={"commandId": "x"})
        assert malformed.status_code == 422


async def test_thread_command_reports_id_conflict_after_runner_admitted_before_app_copied_it(
    app_url: str,
    runner: RunnerHandle,
    event_logs: EventLogStore,
    spec: protocol_pb2.SessionSpec,
    failed_native_journal: None,
    runner_client_factory: RunnerClientFactory,
) -> None:
    """An app prefix lag must still preserve the runner's id-conflict verdict as a 409."""
    thread = await event_logs.open(SANDBOX, SESSION, spec)
    original = command_pb2.Command(
        command_id="reused-before-copy", interrupt_turn=command_pb2.InterruptTurn(turn_id="first-target")
    )
    client = runner_client_factory(runner.target, capture_history=True)
    try:
        attachment = await client.attach(SESSION, spec=spec)
        try:
            await attachment.command(original)
            await attachment.until(lambda entry: entry.event.HasField("command_admitted"))
            await attachment.detach()
            await attachment.drain_until_end()
        finally:
            attachment.cancel()
    finally:
        await client.close()

    async with httpx.AsyncClient(base_url=app_url, timeout=60, headers=AGENT_AUTH) as http:
        conflict = await http.post(
            _commands(str(thread)),
            json={"commandId": "reused-before-copy", "interruptTurn": {"turnId": "second-target"}},
        )
        assert conflict.status_code == 409, conflict.text
        stored = await _stored_events(http, str(thread), until="commandNoop")
        (admitted,) = [entry for entry in stored if "commandAdmitted" in entry["event"]]
        assert admitted["event"]["commandAdmitted"]["command"] == {
            "commandId": "reused-before-copy",
            "interruptTurn": {"turnId": "first-target"},
        }


async def test_stop_command_returns_after_admission_before_native_shutdown_effect(
    app_url: str,
    runner: RunnerHandle,
    spec: protocol_pb2.SessionSpec,
    monkeypatch: pytest.MonkeyPatch,
    failed_native_journal: None,
) -> None:
    async with httpx.AsyncClient(base_url=app_url, timeout=60, headers=AGENT_AUTH) as http:
        opened = await http.post(SESSIONS, json={"session_id": SESSION, "spec": MessageToDict(spec)})
        assert opened.status_code == 201, opened.text
        thread_id = await _thread_id(http)
        session = runner.runner.sessions[SESSION]
        original_shutdown = session._shutdown
        shutdown_entered = asyncio.Event()
        allow_shutdown = asyncio.Event()

        async def gated_shutdown() -> None:
            shutdown_entered.set()
            await allow_shutdown.wait()
            await original_shutdown()

        monkeypatch.setattr(session, "_shutdown", gated_shutdown)
        response = asyncio.create_task(
            http.post(_commands(thread_id), json={"commandId": "stop-gated", "stopRunnerSession": {}})
        )
        try:
            async with asyncio.timeout(10):
                await shutdown_entered.wait()
            # Native shutdown is held above, yet app archival has already made the admission
            # durable and sufficient for the API result.
            async with asyncio.timeout(10):
                accepted = await asyncio.shield(response)
            assert accepted.status_code == 200, accepted.text
            assert accepted.json()["event"]["commandAdmitted"]["command"] == {
                "commandId": "stop-gated",
                "stopRunnerSession": {},
            }
            assert session.running
        finally:
            allow_shutdown.set()
            await asyncio.shield(response)


async def test_command_relay_waits_for_runner_admission_before_closing(
    app_url: str,
    model: ScriptedModel,
    runner: RunnerHandle,
    spec: protocol_pb2.SessionSpec,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A relay cancellation before the runner reads its frames must not discard the Command."""
    async with httpx.AsyncClient(base_url=app_url, timeout=60, headers=AGENT_AUTH) as http:
        opened = await http.post(SESSIONS, json={"session_id": SESSION, "spec": MessageToDict(spec)})
        assert opened.status_code == 201, opened.text
        thread_id = await _thread_id(http)
        original_consume = service._consume
        consumer_started = asyncio.Event()
        release_consumer = asyncio.Event()

        async def gated_consume(
            session: Session,
            requests: AsyncIterator[protocol_pb2.ClientMessage],
            closing: asyncio.Event,
            failure: list[str],
        ) -> None:
            consumer_started.set()
            await release_consumer.wait()
            await original_consume(session, requests, closing, failure)

        monkeypatch.setattr(service, "_consume", gated_consume)
        relay = asyncio.create_task(
            http.post(
                _commands(thread_id),
                json={"commandId": "relay-admission", "submitInput": {"text": "Reply with exactly: RELAY_OK"}},
            )
        )
        try:
            async with asyncio.timeout(10):
                await consumer_started.wait()
            release_consumer.set()
            async with asyncio.timeout(10):
                accepted = await asyncio.shield(relay)
            assert accepted.status_code == 200, accepted.text
            assert accepted.json()["event"]["commandAdmitted"]["command"] == {
                "commandId": "relay-admission",
                "submitInput": {"text": "Reply with exactly: RELAY_OK"},
            }
            request = await model.request()
            assert request.user_texts[-1] == "Reply with exactly: RELAY_OK"
            await model.reply(request, Text("RELAY_OK"))
        finally:
            release_consumer.set()
            if not relay.done():
                relay.cancel()
                await asyncio.gather(relay, return_exceptions=True)


async def test_command_admission_timeout_is_not_an_internal_server_error(
    app_url: str, spec: protocol_pb2.SessionSpec, monkeypatch: pytest.MonkeyPatch, failed_native_journal: None
) -> None:
    async with httpx.AsyncClient(base_url=app_url, timeout=60, headers=AGENT_AUTH) as http:
        opened = await http.post(SESSIONS, json={"session_id": SESSION, "spec": MessageToDict(spec)})
        assert opened.status_code == 201, opened.text
        thread_id = await _thread_id(http)

        async def timed_out(
            _bridge: RunnerBridge, _thread_id: UUID, _command: command_pb2.Command
        ) -> event_log_pb2.EventEntry:
            raise RunnerAdmissionTimeoutError("timed-out-command")

        monkeypatch.setattr(RunnerBridge, "command", timed_out)
        response = await http.post(
            _commands(thread_id), json={"commandId": "timed-out-command", "submitInput": {"text": "not delivered"}}
        )
        assert response.status_code == 504, response.text
        assert "uncertain" in response.json()["detail"]


async def test_command_returns_runner_receipt_before_app_archive_catches_up(
    bridge: RunnerBridge,
    event_logs: EventLogStore,
    content: ContentStore,
    ingestion: Ingestion,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    thread = await event_logs.open(
        SANDBOX,
        SESSION,
        protocol_pb2.SessionSpec(harness=protocol_pb2.HARNESS_CLAUDE, cwd="/state/work", model="bridge-model"),
    )
    command = command_pb2.Command(
        command_id="ahead-of-archive", interrupt_turn=command_pb2.InterruptTurn(turn_id="target")
    )
    timestamp = Timestamp()
    timestamp.GetCurrentTime()
    receipt = event_log_pb2.EventEntry(
        cursor=1,
        origin=event_log_pb2.EventOrigin(source_id="test-runner", sequence=1),
        event=event_pb2.Event(at=timestamp, command_admitted=event_pb2.CommandAdmitted(command=command)),
    )
    called = 0

    async def no_archive_yet() -> None:
        return None

    class Runner:
        async def command(
            self, session_id: str, candidate: command_pb2.Command, *, after_cursor: int
        ) -> event_log_pb2.EventEntry:
            nonlocal called
            called += 1
            assert session_id == SESSION
            assert candidate == command
            assert after_cursor == 0
            return receipt

    monkeypatch.setattr(bridge._ingester, "start", no_archive_yet)
    monkeypatch.setattr(bridge._runners, "client", lambda _sandbox: Runner())
    assert await bridge.command(thread, command) == receipt
    assert await content.admitted_command(thread, command) is None
    assert await event_logs.events(thread, limit=10) == []

    lease = await ingestion.acquire(SANDBOX, timedelta(minutes=1))
    assert lease is not None
    await ingestion.record(thread, [receipt], lease=lease)
    assert await content.admitted_command(thread, command) == receipt
    # The archive, not the runner, answers exact retries once it has caught up.
    assert await bridge.command(thread, command) == receipt
    assert called == 1


@dataclass
class Replicas:
    owner: RunnerBridge
    owner_ingester: Ingester
    survivor: RunnerBridge
    survivor_ingester: Ingester
    # What the survivor streams a thread from: its own pool and its own listener.
    survivor_event_logs: EventLogStore
    survivor_changes: Changes


@pytest.fixture
async def replicas(
    sandbox_endpoint: Endpoint,
    local_runners: SandboxSessions,
    live_index: LiveIndex,
    runner: RunnerHandle,
    database_updates: DatabaseUpdates,
    db_url: str,
    event_logs: EventLogStore,
    content: ContentStore,
    ingestion: Ingestion,
) -> AsyncIterator[Replicas]:
    replica_engine = connect(db_url)
    replica_updates = DatabaseUpdates(replica_engine.url)
    survivor_runners = SandboxSessions(live_index, sandbox_endpoint.client())
    survivor_event_logs = EventLogStore(replica_engine)
    owner_ingester = Ingester(runners=local_runners, event_logs=event_logs, ingestion=ingestion)
    survivor_ingester = Ingester(
        runners=survivor_runners, event_logs=survivor_event_logs, ingestion=Ingestion(replica_engine)
    )
    owner = RunnerBridge(runners=local_runners, event_logs=event_logs, content=content, ingester=owner_ingester)
    survivor = RunnerBridge(
        runners=survivor_runners,
        event_logs=survivor_event_logs,
        content=ContentStore(replica_engine),
        ingester=survivor_ingester,
    )
    try:
        async with replica_updates.listener.listen():
            await owner_ingester.start()
            await owner_ingester.reconcile()
            try:
                yield Replicas(
                    owner,
                    owner_ingester,
                    survivor,
                    survivor_ingester,
                    survivor_event_logs,
                    replica_updates.changes[Channel.THREADS],
                )
            finally:
                await owner_ingester.close()
                await survivor_ingester.close()
                await survivor_runners.close()
    finally:
        await replica_engine.dispose()


async def frame_lines(frames: AsyncIterator[bytes]) -> AsyncIterator[str]:
    async for frame in frames:
        for line in frame.decode().splitlines():
            yield line


async def test_semantic_feed_failure_survives_replica_reconcile(
    runner: RunnerHandle,
    local_runners: SandboxSessions,
    event_logs: EventLogStore,
    ingestion: Ingestion,
    db_url: str,
    spec: protocol_pb2.SessionSpec,
    monkeypatch: pytest.MonkeyPatch,
    runner_client_factory: RunnerClientFactory,
) -> None:
    """A new app owner cannot overwrite a rejected prefix's persisted failure with active."""
    client = runner_client_factory(runner.target, capture_history=True)
    replica_engine = connect(db_url)
    replica_store, replica_event_logs = ThreadStore(replica_engine), EventLogStore(replica_engine)
    replica_updates = DatabaseUpdates(replica_engine.url)
    try:
        async with replica_updates.listener.listen():
            attachment = await client.attach(SESSION, spec=spec)
            try:
                await attachment.detach()
                await attachment.drain_until_end()
                assert attachment.seen
                attachment.seen[-1].event.at.seconds += 1
                thread = await event_logs.open(SANDBOX, SESSION, spec)
                lease = await ingestion.acquire(SANDBOX, timedelta(minutes=1))
                assert lease is not None
                await ingestion.record(thread, attachment.seen, lease=lease)
                async with asyncio.timeout(10):
                    await Feed(
                        session_id=SESSION,
                        client=local_runners.client(SANDBOX),
                        event_logs=event_logs,
                        ingestion=ingestion,
                        lease=lease,
                    ).run()
                failed = await replica_event_logs.feed_state(thread)
                assert failed is not None
                assert failed.end == FeedError(f"conflicting runner entry at cursor {attachment.seen[-1].cursor}")
                assert await event_logs.events(thread, limit=len(attachment.seen) + 1) == attachment.seen
                async with replica_store._sessions() as session:
                    checkpoint = await session.get(ThreadCheckpoint, thread)
                    assert checkpoint is not None
                    view = await session.get(
                        ThreadEntity, (thread, checkpoint.projection_epoch, "view_state", "current")
                    )
                    assert view is not None
                    operational = ThreadOperationalState.model_validate(view.state["operational"])
                assert operational.feed_error is not None
                assert operational.feed_error.cursor == str(attachment.seen[-1].cursor)
                await ingestion.release(lease)
            finally:
                attachment.cancel()

            survivor_ingester = Ingester(
                runners=local_runners, event_logs=replica_event_logs, ingestion=Ingestion(replica_engine)
            )
            survivor = RunnerBridge(
                runners=local_runners,
                event_logs=replica_event_logs,
                content=ContentStore(replica_engine),
                ingester=survivor_ingester,
            )
            try:
                await survivor_ingester.start()
                await survivor_ingester.reconcile()
                assert not survivor_ingester._feeds
                assert await replica_event_logs.feed_state(thread) == failed

                dispatched = False

                async def reject_dispatch(*_args: object, **_kwargs: object) -> None:
                    nonlocal dispatched
                    dispatched = True

                monkeypatch.setattr(survivor, "_command", reject_dispatch)
                with pytest.raises(RunnerError):
                    await survivor.command(
                        thread,
                        command_pb2.Command(
                            command_id="must-not-reach-rejected-runner",
                            submit_input=command_pb2.SubmitInput(text="must not dispatch"),
                        ),
                    )
                assert not dispatched

                reattached = False

                async def reject_attach(*_args: object, **_kwargs: object) -> None:
                    nonlocal reattached
                    reattached = True
                    raise AssertionError("a rejected feed must refuse reopen before native attach")

                # Patched on the class, not the bridge: the survivor's discovery loop keeps listing the
                # runner's sessions meanwhile, and it must not reattach the rejected one either.
                monkeypatch.setattr(RunnerClient, "attach", reject_attach)
                with pytest.raises(RunnerError):
                    await survivor.open_session(SANDBOX, SESSION, spec)
                assert not reattached
            finally:
                await survivor_ingester.close()
    finally:
        await replica_engine.dispose()
        await client.close()


async def test_ingestion_reports_truncated_replay_instead_of_normal_completion(
    runner: RunnerHandle,
    local_runners: SandboxSessions,
    event_logs: EventLogStore,
    ingestion: Ingestion,
    spec: protocol_pb2.SessionSpec,
    monkeypatch: pytest.MonkeyPatch,
    runner_client_factory: RunnerClientFactory,
) -> None:
    client = runner_client_factory(runner.target, capture_history=True)
    try:
        attachment = await client.attach(SESSION, spec=spec)
        try:
            await attachment.detach()
            await attachment.drain_until_end()
        finally:
            attachment.cancel()
        thread = await event_logs.open(SANDBOX, SESSION, spec)
        lease = await ingestion.acquire(SANDBOX, timedelta(minutes=1))
        assert lease is not None

        async def truncated_stream(attachment: Attachment) -> event_log_pb2.EventEntry:
            assert attachment.attached.last_cursor > 0
            raise StreamClosedError

        monkeypatch.setattr(Attachment, "next_entry", truncated_stream)
        async with asyncio.timeout(10):
            await Feed(
                session_id=SESSION,
                client=local_runners.client(SANDBOX),
                event_logs=event_logs,
                ingestion=ingestion,
                lease=lease,
            ).run()
        snapshot = await event_logs.feed_state(thread)
        assert snapshot is not None
        assert isinstance(snapshot.end, FeedError)
        assert await event_logs.last_cursor(thread) == 0
    finally:
        await client.close()


async def test_replica_commands_and_database_stream_survive_ingestion_owner_exit(
    replicas: Replicas, event_logs: EventLogStore, model: ScriptedModel, spec: protocol_pb2.SessionSpec
) -> None:
    await replicas.owner.open_session(SANDBOX, SESSION, spec)
    thread = await event_logs.open(SANDBOX, SESSION, spec)
    async with aclosing(
        follow(replicas.survivor_event_logs, replicas.survivor_changes, thread, after_cursor=0)
    ) as frames:
        lines = frame_lines(frames)
        assert (await next_message(lines)).event == "attached"
        await replicas.survivor.command(
            thread,
            command_pb2.Command(command_id="input-1", submit_input=command_pb2.SubmitInput(text="FIRST_REPLICA_TURN")),
        )
        await model.reply(await model.request(), Text("FIRST_REPLICA_TURN"))
        async with asyncio.timeout(10):
            first = await read_until(lines, "turnCompleted")

        await replicas.survivor.command(
            thread,
            command_pb2.Command(command_id="input-2", submit_input=command_pb2.SubmitInput(text="AFTER_OWNER_EXIT")),
        )
        request = await model.request()
        await replicas.owner_ingester.close()
        await model.reply(request, Text("AFTER_OWNER_EXIT"))
        async with asyncio.timeout(10):
            second = await read_until(lines, "turnCompleted")
        seen = [*first, *second]
        assert [message.id for message in seen] == list(range(1, len(seen) + 1))
        assert [
            message.data["event"]["itemCompleted"]["text"]
            for message in seen
            if "itemCompleted" in message.data["event"]
        ] == ["FIRST_REPLICA_TURN", "AFTER_OWNER_EXIT"]
        await replicas.survivor.command(
            thread,
            command_pb2.Command(command_id="stop-after-owner", stop_runner_session=command_pb2.StopRunnerSession()),
        )
        async with asyncio.timeout(10):
            await read_until(lines, "harnessExited")
            assert (await next_message(lines)).event == "end"


async def test_inventory_change_discovers_existing_runner_session_without_browser_open(
    sandbox_endpoint: Endpoint,
    custom_objects: FakeCustomObjectsApi,
    core_v1: FakeCoreV1Api,
    runner: RunnerHandle,
    store: ThreadStore,
    event_logs: EventLogStore,
    model: ScriptedModel,
    spec: protocol_pb2.SessionSpec,
    monkeypatch: pytest.MonkeyPatch,
    ingestion: Ingestion,
    live_index: LiveIndex,
    runner_client_factory: RunnerClientFactory,
) -> None:
    # This must wake from the informer notification, not the periodic recovery scan.
    monkeypatch.setattr("agentplane.app.threads.ingestion.RECONCILE_S", 3600)
    runners = SandboxSessions(live_index, sandbox_endpoint.client())
    discovered = asyncio.Event()
    running = runners.running

    def observed_running() -> set[str]:
        discovered.set()
        return running()

    monkeypatch.setattr(runners, "running", observed_running)
    ingester = Ingester(runners=runners, event_logs=event_logs, ingestion=ingestion)
    client = runner_client_factory(runner.target, capture_history=True)
    try:
        async with await client.attach(SESSION, spec=spec):
            pass
        await ingester.start()
        async with asyncio.timeout(10):
            await discovered.wait()
        discovered.clear()
        live_index.sandboxes[SANDBOX], live_index.pods[SANDBOX] = seed_runner(custom_objects, core_v1, SANDBOX)
        live_index.changes.notify()
        async with asyncio.timeout(10):
            await discovered.wait()
        # Only the ingester can create this row: no bridge exists, so no Open, command, or SSE request ran.
        async for attempt in AsyncRetrying(
            stop=stop_after_delay(10), wait=wait_fixed(0.1), retry=retry_if_exception_type(AssertionError)
        ):
            with attempt:
                threads = await store.list_threads()
                assert len(threads) == 1
                assert await event_logs.last_cursor(threads[0].id) > 0
        assert threads[0].sandbox == SANDBOX
        assert threads[0].session_id == SESSION
    finally:
        await ingester.close()
        await runners.close()
        await client.close()


async def test_resumed_session_stream_does_not_end_at_previous_shutdown(
    replicas: Replicas, event_logs: EventLogStore, model: ScriptedModel, spec: protocol_pb2.SessionSpec
) -> None:
    await replicas.owner.open_session(SANDBOX, SESSION, spec)
    thread = await event_logs.open(SANDBOX, SESSION, spec)
    await replicas.survivor.command(
        thread, command_pb2.Command(command_id="seed-input", submit_input=command_pb2.SubmitInput(text="BEFORE_RESUME"))
    )
    await model.reply(await model.request(), Text("BEFORE_RESUME"))
    async with aclosing(
        follow(replicas.survivor_event_logs, replicas.survivor_changes, thread, after_cursor=0)
    ) as frames:
        async with asyncio.timeout(10):
            await read_until(frame_lines(frames), "turnCompleted")
    await replicas.survivor.command(
        thread,
        command_pb2.Command(command_id="stop-before-resume", stop_runner_session=command_pb2.StopRunnerSession()),
    )
    async with aclosing(
        follow(replicas.survivor_event_logs, replicas.survivor_changes, thread, after_cursor=0)
    ) as frames:
        lines = frame_lines(frames)
        assert (await next_message(lines)).event == "attached"
        async with asyncio.timeout(10):
            stopped = await read_until(lines, "harnessExited")
            assert (await next_message(lines)).event == "end"
    cursor = stopped[-1].id
    assert cursor is not None
    await replicas.survivor.open_session(SANDBOX, SESSION, spec)
    async with aclosing(
        follow(replicas.survivor_event_logs, replicas.survivor_changes, thread, after_cursor=cursor)
    ) as frames:
        lines = frame_lines(frames)
        assert (await next_message(lines)).event == "attached"
        await replicas.survivor.command(
            thread,
            command_pb2.Command(
                command_id="resumed-input", submit_input=command_pb2.SubmitInput(text="RESUMED_REPLICA")
            ),
        )
        await model.reply(await model.request(), Text("RESUMED_REPLICA"))
        async with asyncio.timeout(10):
            resumed = await read_until(lines, "turnCompleted")
        assert all(message.event == "event" for message in resumed)
        assert any(message.data["event"].get("harnessStarted", {}).get("resumed") for message in resumed)
        assert [message.id for message in resumed] == list(range(cursor + 1, cursor + len(resumed) + 1))
        await replicas.survivor.command(
            thread,
            command_pb2.Command(command_id="stop-after-resume", stop_runner_session=command_pb2.StopRunnerSession()),
        )


async def test_stored_thread_stream_does_not_require_reachable_runner(
    replicas: Replicas,
    local_runners: SandboxSessions,
    live_index: LiveIndex,
    event_logs: EventLogStore,
    database_updates: DatabaseUpdates,
    spec: protocol_pb2.SessionSpec,
    content: ContentStore,
    ingestion: Ingestion,
) -> None:
    await replicas.owner.open_session(SANDBOX, SESSION, spec)
    thread = await event_logs.open(SANDBOX, SESSION, spec)
    stop = command_pb2.Command(command_id="stop-for-offline", stop_runner_session=command_pb2.StopRunnerSession())
    admitted = await replicas.survivor.command(thread, stop)
    async with aclosing(
        follow(replicas.survivor_event_logs, replicas.survivor_changes, thread, after_cursor=0)
    ) as frames:
        lines = frame_lines(frames)
        await next_message(lines)
        async with asyncio.timeout(10):
            stored = await read_until(lines, "harnessExited")
            assert (await next_message(lines)).event == "end"
    await replicas.owner_ingester.close()
    await replicas.survivor_ingester.close()

    del live_index.sandboxes[SANDBOX], live_index.pods[SANDBOX]
    offline_ingester = Ingester(runners=local_runners, event_logs=event_logs, ingestion=ingestion)
    offline = RunnerBridge(runners=local_runners, event_logs=event_logs, content=content, ingester=offline_ingester)
    try:
        # A lost HTTP response is retryable from the committed Thread prefix even after the
        # sandbox disappears: this answer must not attempt a new runner attachment, which with the
        # sandbox gone from the index would raise.
        assert await offline.command(thread, stop) == admitted
        async with (
            asyncio.timeout(10),
            aclosing(follow(event_logs, database_updates.changes[Channel.THREADS], thread, after_cursor=0)) as frames,
        ):
            lines = frame_lines(frames)
            assert (await next_message(lines)).event == "attached"
            assert await read_until(lines, "harnessExited") == stored
            assert (await next_message(lines)).event == "end"
    finally:
        await offline_ingester.close()


async def test_open_and_resume_reply_before_archive_catches_up(
    local_runners: SandboxSessions,
    event_logs: EventLogStore,
    content: ContentStore,
    ingestion: Ingestion,
    model: ScriptedModel,
    spec: protocol_pb2.SessionSpec,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Runner receipts and a committed Thread mapping suffice; copying the feed is independent."""
    ingester = Ingester(runners=local_runners, event_logs=event_logs, ingestion=ingestion)
    # Simulate stalled ingestion without delaying the runner itself.
    monkeypatch.setattr(ingester, "start", AsyncMock())
    bridge = RunnerBridge(runners=local_runners, event_logs=event_logs, content=content, ingester=ingester)
    opened = await asyncio.wait_for(bridge.open_session(SANDBOX, SESSION, spec), timeout=10)
    thread_id = await event_logs.find(SANDBOX, SESSION)
    assert thread_id is not None
    assert opened.last_cursor > 0
    assert await event_logs.last_cursor(thread_id) == 0

    # A native conversation must exist before Resume: an idle Open has no Claude/Codex
    # history to restore even if the runner subsequently reports STOPPED.
    seed = await bridge.command(
        thread_id,
        command_pb2.Command(command_id="seed-before-archive", submit_input=command_pb2.SubmitInput(text="SEED")),
    )
    await model.reply(await model.request(), Text("SEED_REPLY"))
    attachment = await local_runners.client(SANDBOX).attach(SESSION, after_cursor=seed.cursor)
    try:
        async with asyncio.timeout(20):
            while (await attachment.next_entry()).event.WhichOneof("observation") != "turn_completed":
                pass
    finally:
        attachment.cancel()
    assert await event_logs.last_cursor(thread_id) == 0

    await bridge.command(
        thread_id,
        command_pb2.Command(command_id="stop-before-archive", stop_runner_session=command_pb2.StopRunnerSession()),
    )
    # Admission is not harness shutdown. Wait on the runner's own state, not on the
    # stalled archive, before attempting a legitimate Resume.
    async for attempt in AsyncRetrying(
        stop=stop_after_delay(10), wait=wait_fixed(0.1), retry=retry_if_exception_type(AssertionError)
    ):
        with attempt:
            (summary,) = await local_runners.client(SANDBOX).list_sessions()
            assert summary.harness_state == protocol_pb2.HARNESS_STATE_STOPPED
    resumed = await asyncio.wait_for(
        bridge.resume_thread(
            thread_id, expected_harness=protocol_pb2.Harness.Name(spec.harness), expected_cwd=spec.cwd
        ),
        timeout=10,
    )
    assert resumed.session_id == SESSION
    assert resumed.last_cursor > opened.last_cursor
    assert await event_logs.last_cursor(thread_id) == 0


if __name__ == "__main__":
    pytest_bazel.main()
