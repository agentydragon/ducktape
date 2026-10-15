"""One browser-shaped script over the bridge against a local runner, run for both harnesses: open a
session, stream it, send an input while streaming, open a second tab on the same session, reconnect
from the last event id, shut down; and the thread the store kept of all of it."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import signal
from collections.abc import AsyncIterator, Callable
from contextlib import aclosing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, override
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import grpc
import httpx
import pytest
import pytest_bazel
from google.protobuf.json_format import MessageToDict
from google.protobuf.timestamp_pb2 import Timestamp
from sqlalchemy import select
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_delay, wait_fixed

from agentplane.app.action_policy import ActionPolicyInventory
from agentplane.app.api import create_app
from agentplane.app.conftest import AGENT_AUTH, listening
from agentplane.app.database import connect
from agentplane.app.database_updates import Channel
from agentplane.app.egress_access import EgressAccess
from agentplane.app.electric import ElectricProxy
from agentplane.app.identity import TokenReviewer
from agentplane.app.live import LiveIndex
from agentplane.app.operator_sessions import OperatorSessionStore
from agentplane.app.sandbox_access import SandboxAccess
from agentplane.app.testing.dispatcher import GatewayBackedDispatcher
from agentplane.app.testing.history import SeededEventLogStore, register_gateway_session
from agentplane.app.testing.model_test_data import TEST_MODELS
from agentplane.app.testing.system_status import answering_system_status
from agentplane.app.threads.bridge import DispatcherRequestTimeoutError, DispatchNotFoundError, RunnerBridge
from agentplane.app.threads.dispatcher_client import SessionDispatcherClient
from agentplane.app.threads.events.stream import follow
from agentplane.app.threads.history_watch import SessionOpenings
from agentplane.app.threads.runner_directory import RunnerDirectory
from agentplane.app.threads.store import ThreadStore
from agentplane.app.threads.summaries import Summaries
from agentplane.app.threads.view.content import ContentStore
from agentplane.changes import ChangeSignal
from agentplane.egress.decisions_client import DecisionsClient
from agentplane.grpc_calls import dial
from agentplane.grpc_serving import serving
from agentplane.history_service.client import HistoryServiceClient
from agentplane.history_service.fold.models import ThreadCheckpoint, ThreadEntity, ThreadPayloadChunk
from agentplane.history_service.testing.backend import history_service, ingest
from agentplane.history_service.testing.history import History
from agentplane.postgres.wakeups import WakeupListener
from agentplane.proto import (
    command_pb2,
    event_log_pb2,
    event_pb2,
    runner_pb2,
    session_dispatcher_pb2,
    session_dispatcher_pb2_grpc,
)
from agentplane.proto.sandboxes_pb2 import SandboxDestination, ServiceAccount
from agentplane.runner.harness import Harness
from agentplane.runner.runner import Runner
from agentplane.runner.session import Session
from agentplane.runner.testing.failure_journal import FailureEvidence
from agentplane.runner.testing.scripted_model import ScriptedModel, ShellCall, Text
from agentplane.runner_gateway.client import RunnerGatewayClient
from agentplane.runner_gateway.testing.backend import Endpoint, runner_bearer
from agentplane.runner_gateway.testing.control_listener import dialing
from agentplane.sandboxes.testing.fake_inventory import NAMESPACE, FakeCoreV1Api, FakeCustomObjectsApi, seed_runner
from agentplane.subjects import ServiceAccountRef
from util.net import bind_free_port
from util.testing.asgi import serve_app_in_loop

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf

SANDBOX = "bridge-test-sandbox"
SESSION = "8e414bdd-1310-4687-b768-3f1d69047122"
SESSIONS = f"/sandboxes/{SANDBOX}/sessions"


@pytest.fixture
async def failed_bridge_state(
    request: pytest.FixtureRequest,
    runner: Runner,
    event_logs: SeededEventLogStore,
    history_client: HistoryServiceClient,
    local_runners: RunnerDirectory,
) -> AsyncIterator[None]:
    """Preserve each service's cursor and admission state when an app-level bridge case fails.

    The `runner` fixture keeps the native history files and journal.
    """
    yield
    if (evidence := FailureEvidence.for_failed_call(request)) is None:
        return
    state: dict[str, Any] = {}
    for session_id, session in runner.sessions.items():
        observed: dict[str, Any] = {"runner_cursor": session.journal.last_cursor}
        state[session_id] = observed
        try:
            async with asyncio.timeout(5):
                thread_id = UUID(session_id)
                observed["app_cursor"] = await event_logs.last_cursor(thread_id)
                snapshot = await event_logs.feed_state(thread_id)
                observed["app_feed"] = (
                    None
                    if snapshot is None
                    else {"attached": MessageToDict(snapshot.attached), "end": repr(snapshot.end)}
                )
                page = await history_client.read_session_events(session_id, after_cursor=0, limit=1)
                observed["history_cursor"] = page.last_cursor
                observed["history_feed"] = MessageToDict(page.feed_state) if page.HasField("feed_state") else None
        except Exception as error:
            observed["history_error"] = repr(error)
            logging.getLogger(__name__).warning("bridge failure history state capture failed", exc_info=True)
    evidence.write_json("bridge-state.json", state)


async def _create(http: httpx.AsyncClient, spec: runner_pb2.SessionSpec, session_id: UUID) -> str:
    """Create a Session through the app; its public ID is the Thread's."""
    opened = await http.post(SESSIONS, json={"session_id": str(session_id), "spec": MessageToDict(spec)})
    assert opened.status_code == 201, opened.text
    return str(opened.json()["sessionId"])


async def test_new_session_always_uses_dispatcher_over_grpc(tmp_path: Path, bridge: RunnerBridge) -> None:
    session_id = uuid4()
    spec = runner_pb2.SessionSpec(harness=runner_pb2.HARNESS_CODEX, model="test-model")
    received: list[session_dispatcher_pb2.OpenSessionRequest] = []

    class QueuedOpenDispatcher(session_dispatcher_pb2_grpc.SessionDispatcherServicer):
        @override
        async def OpenSession(
            self, request: session_dispatcher_pb2.OpenSessionRequest, _context: grpc.aio.ServicerContext
        ) -> session_dispatcher_pb2.OpenSessionResponse:
            received.append(request)
            return session_dispatcher_pb2.OpenSessionResponse(
                opening=session_dispatcher_pb2.SessionOpening(
                    session_id=request.session_id,
                    sandbox=request.sandbox,
                    state=session_dispatcher_pb2.DELIVERY_STATE_QUEUED_IN_DISPATCHER,
                    reason="sandbox_resuming",
                )
            )

    server = grpc.aio.server()
    session_dispatcher_pb2_grpc.add_SessionDispatcherServicer_to_server(QueuedOpenDispatcher(), server)
    token_file = tmp_path / "dispatcher-token"
    await asyncio.to_thread(token_file.write_text, "test-app-command-peer")
    async with serving(server, "127.0.0.1:0", grace_s=0) as port, dial(f"127.0.0.1:{port}") as channel:
        bridge._dispatcher = SessionDispatcherClient(channel, token_file=token_file, request_timeout_s=5)

        result = await bridge.create_session(SANDBOX, session_id, spec, "bootstrap")

    assert result.session_id == str(session_id)
    assert result.sandbox == SANDBOX
    assert result.state == session_dispatcher_pb2.DELIVERY_STATE_QUEUED_IN_DISPATCHER
    assert result.reason == "sandbox_resuming"
    assert len(received) == 1
    assert received[0].session_id == str(session_id)
    assert received[0].sandbox == SANDBOX
    assert received[0].spec == spec
    assert received[0].setup_script == "bootstrap"


def _runner_session(runner: Runner, thread_id: str | UUID) -> Session:
    return runner.sessions[str(thread_id)]


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
        try:
            message = await next_message(lines)
        except AssertionError as error:
            progress = [(row.event, row.id, list(row.data.get("event", {}))) for row in seen[-20:]]
            raise AssertionError(f"transport EOF before {key}; preceding SSE frames: {progress}") from error
        seen.append(message)
        if message.event in ("end", "error"):
            progress = [(row.event, row.id, list(row.data.get("event", {}))) for row in seen[-20:]]
            raise AssertionError(f"SSE {message.event} before {key}; preceding SSE frames: {progress}")
        if key in message.data.get("event", {}):
            return seen


@pytest.fixture
def register_attached_sessions() -> bool:
    # The local runner pushes its history to the History Service ingress; the app only reads it.
    return True


@pytest.fixture
async def history_client(
    runner: Runner, service_history_db_url: str, history_token_file: Path
) -> AsyncIterator[HistoryServiceClient]:
    # The History Service reads what the local runner pushes to its ingress, as deployed.
    async with (
        ingest(service_history_db_url, runner, writer=ServiceAccountRef(namespace=NAMESPACE, name=SANDBOX)),
        history_service(service_history_db_url, history_token_file) as client,
    ):
        yield client


@pytest.fixture
async def local_runners(
    live_index: LiveIndex,
    gateway_endpoint: Endpoint,
    gateway: RunnerGatewayClient,
    custom_objects: FakeCustomObjectsApi,
    core_v1: FakeCoreV1Api,
    runner: Runner,
) -> AsyncIterator[RunnerDirectory]:
    """`SANDBOX` running, its Pod's runner connected over the control stream."""
    raw, live_index.pods[SANDBOX] = seed_runner(custom_objects, core_v1, SANDBOX)
    live_index.sandboxes[SANDBOX] = raw
    async with dialing(runner, gateway_endpoint.control_target, runner_bearer(SANDBOX)):
        await asyncio.to_thread(gateway_endpoint.wait_connected, UUID(raw["metadata"]["uid"]))
        yield RunnerDirectory(live_index, gateway)


@pytest.fixture
async def app_url(
    local_runners: RunnerDirectory,
    inventory: SandboxAccess,
    store: ThreadStore,
    database_updates: WakeupListener,
    operator_sessions: OperatorSessionStore,
    egress: EgressAccess,
    decisions: DecisionsClient,
    live_index: LiveIndex,
    action_policy: ActionPolicyInventory,
    reviewer: TokenReviewer,
    event_logs: SeededEventLogStore,
    content: ContentStore,
    history: History,
    electric_proxy: ElectricProxy,
    notifications_http: httpx.AsyncClient,
    notifications_token_file: Path,
) -> AsyncIterator[str]:
    """The app served by uvicorn, with the one test sandbox resolving to the local runner. The
    server is real because SSE needs a response that streams, which an in-process ASGI transport
    would buffer."""
    bridge = RunnerBridge(
        runners=local_runners,
        event_logs=event_logs,
        content=content,
        dispatcher=GatewayBackedDispatcher(local_runners, event_logs),
    )
    app = create_app(
        inventory,
        bridge,
        store,
        TEST_MODELS,
        egress,
        decisions,
        live_index,
        action_policy,
        reviewer=reviewer,
        event_logs=event_logs,
        content=content,
        database_updates=database_updates,
        operator_sessions=operator_sessions,
        electric=electric_proxy,
        notifications_http=notifications_http,
        notifications_token_file=notifications_token_file,
        system_status=answering_system_status(),
    )
    sock = bind_free_port()
    async with serve_app_in_loop(app, sock=sock):
        yield f"http://127.0.0.1:{sock.getsockname()[1]}"


async def test_the_bridge_streams_a_turn_to_every_tab_and_resumes_from_the_last_event_id(
    app_url: str, model: ScriptedModel, spec: runner_pb2.SessionSpec, failed_bridge_state: None
) -> None:
    async with httpx.AsyncClient(base_url=app_url, timeout=60, headers=AGENT_AUTH) as http:
        session_id = uuid4()
        opened = await http.post(SESSIONS, json={"session_id": str(session_id), "spec": MessageToDict(spec)})
        assert opened.status_code == 201, opened.text
        assert opened.json()["state"] == "DELIVERY_STATE_RUNNER_ADMITTED"
        assert opened.json()["attached"]["harnessState"] == "HARNESS_STATE_RUNNING"
        thread_id = opened.json()["sessionId"]
        async for attempt in AsyncRetrying(
            stop=stop_after_delay(10), wait=wait_fixed(0.05), retry=retry_if_exception_type(AssertionError)
        ):
            with attempt:
                assert [row["sessionId"] for row in (await http.get(SESSIONS)).json()] == [thread_id]

        async for attempt in AsyncRetrying(
            stop=stop_after_delay(10), wait=wait_fixed(0.05), retry=retry_if_exception_type(AssertionError)
        ):
            with attempt:
                assert (await http.get(f"/threads/{thread_id}")).json()["last_cursor"] > 0
        async with http.stream("GET", f"/threads/{thread_id}/events/stream") as first_tab:
            first = first_tab.aiter_lines()
            assert (await next_message(first)).event == "attached"
            assert await _create(http, spec, session_id) == thread_id
            accepted = await http.post(
                _commands(thread_id),
                json={"commandId": "input-1", "submitInput": {"text": "Reply with exactly: BRIDGE_OK"}},
            )
            assert accepted.status_code == 200, accepted.text
            assert accepted.json()["admission"]["event"]["commandAdmitted"]["command"] == {
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
                assert accepted.json()["admission"]["event"]["commandAdmitted"]["command"] == {
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
        assert stopped.json()["admission"]["event"]["commandAdmitted"]["command"] == {
            "commandId": "stop-bridge",
            "stopRunnerSession": {},
        }

        # The store kept the whole thread, readable without the runner: both turns, the raw
        # frames, and the exit the shutdown caused.
        (thread,) = (await http.get("/threads")).json()["threads"]
        assert thread["id"] == thread_id
        assert (thread["sandbox"], thread["model"]) == (SANDBOX, spec.model)
        stored = await _stored_events(http, thread["id"], until="harnessExited")
        assert [entry["cursor"] for entry in stored] == [str(n) for n in range(1, len(stored) + 1)]
        assert [entry["event"]["itemCompleted"]["text"] for entry in stored if "itemCompleted" in entry["event"]] == [
            "BRIDGE_OK",
            "BRIDGE_TWO",
        ]
        assert any("native" in entry["event"] for entry in stored)
        async for attempt in AsyncRetrying(
            stop=stop_after_delay(10), wait=wait_fixed(0.05), retry=retry_if_exception_type(AssertionError)
        ):
            with attempt:
                assert (await http.get(f"/threads/{thread['id']}")).json()["last_cursor"] == len(stored)
        (summary,) = (await http.get(SESSIONS)).json()
        assert summary["harnessState"] == "HARNESS_STATE_STOPPED"

        resumed = await http.post(f"/threads/{thread_id}/resume")
        assert resumed.status_code == 200, resumed.text
        assert resumed.json()["sessionId"] == thread_id
        assert resumed.json()["spec"]["harness"] == runner_pb2.Harness.Name(spec.harness)
        assert resumed.json()["harnessState"] == "HARNESS_STATE_RUNNING"
        assert (await http.get("/threads")).json()["threads"][0]["id"] == thread_id

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
    """The thread's archived entries, replayed from the start up to the first one carrying `until`
    after which `matches` holds; the feed archives them a moment after the runner emitted them."""
    entries: list[dict[str, Any]] = []
    async with asyncio.timeout(30), http.stream("GET", f"/threads/{thread_id}/events/stream") as response:
        assert response.status_code == 200
        lines = response.aiter_lines()
        while not (any(until in entry["event"] for entry in entries) and (matches is None or matches(entries))):
            message = await next_message(lines)
            if message.event == "event":
                entries.append(message.data)
    return entries


async def _folded_items(history: History, thread_id: str) -> tuple[dict[str, ThreadEntity], list[ThreadPayloadChunk]]:
    """The current item rows and payload chunks from the History Service's thread fold."""
    async with history.sessions() as session:
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
    history: History, thread_id: str, matches: Callable[[dict[str, ThreadEntity], list[ThreadPayloadChunk]], bool]
) -> tuple[dict[str, ThreadEntity], list[ThreadPayloadChunk]]:
    async for attempt in AsyncRetrying(
        stop=stop_after_delay(10), wait=wait_fixed(0.2), retry=retry_if_exception_type(AssertionError)
    ):
        with attempt:
            items, chunks = await _folded_items(history, thread_id)
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
    http: httpx.AsyncClient, model: ScriptedModel, spec: runner_pb2.SessionSpec, *, seed_prompt: str, seed_answer: str
) -> str:
    thread_id = await _create(http, spec, uuid4())
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
    app_url: str, model: ScriptedModel, spec: runner_pb2.SessionSpec, failed_bridge_state: None
) -> None:
    """A failed model turn stays admitted and replayable; a later input is a separate turn."""
    async with httpx.AsyncClient(base_url=app_url, timeout=60, headers=AGENT_AUTH) as http:
        thread_id = await _create(http, spec, uuid4())
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
        async for attempt in AsyncRetrying(
            stop=stop_after_delay(10), wait=wait_fixed(0.05), retry=retry_if_exception_type(AssertionError)
        ):
            with attempt:
                assert (await http.get(f"/threads/{thread_id}")).json()["last_turn_status"] == "TURN_STATUS_FAILED"
        # Re-reading the archive, as on a UI reload, retains the same terminal evidence.
        assert (
            await _stored_events(
                http,
                thread_id,
                until="turnCompleted",
                matches=lambda entries: _has_turn_status(entries, "TURN_STATUS_FAILED"),
            )
            == failed
        )
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
        async for attempt in AsyncRetrying(
            stop=stop_after_delay(10), wait=wait_fixed(0.05), retry=retry_if_exception_type(AssertionError)
        ):
            with attempt:
                assert (await http.get(f"/threads/{thread_id}")).json()["last_turn_status"] == "TURN_STATUS_COMPLETED"


@pytest.mark.parametrize("stop_mode", ["interrupt", "shutdown", "kill"])
async def test_stream_recovery_reports_the_content_the_model_receives(
    app_url: str,
    model: ScriptedModel,
    runner: Runner,
    history: History,
    spec: runner_pb2.SessionSpec,
    failed_bridge_state: None,
    stop_mode: str,
) -> None:
    seed_prompt, seed_answer = "Remember the seed prompt", "SEED_ANSWER_FOR_STREAM_RECOVERY"
    partial = "PARTIAL_STREAM_MUST_STAY_UNFINISHED"
    async with httpx.AsyncClient(base_url=app_url, timeout=60, headers=AGENT_AUTH) as http:
        thread_id = await _open_thread_with_seed(http, model, spec, seed_prompt=seed_prompt, seed_answer=seed_answer)
        accepted = await http.post(
            _commands(thread_id), json={"commandId": "streaming-input", "submitInput": {"text": "Start a long answer"}}
        )
        assert accepted.status_code == 200, accepted.text
        request = await _next_model_request(model)
        await model.hold_after_first_delta(request, Text(partial))
        items, chunks = await _wait_for_folded_items(
            history,
            thread_id,
            lambda rows, payloads: any(_folded_body(payloads, row.text_ref) == partial for row in rows.values()),
        )
        item_id = next(row.entity_id for row in items.values() if _folded_body(chunks, row.text_ref) == partial)
        assert items[item_id].state["completion"] is None
        recovery_cursor = await _interrupt_and_recover(http, runner, thread_id, stop_mode)
        items, chunks = await _wait_for_folded_items(
            history,
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


async def _interrupt_and_recover(http: httpx.AsyncClient, runner: Runner, thread_id: str, mode: str) -> int:
    session = _runner_session(runner, thread_id)
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
        assert resumed.json()["sessionId"] == thread_id
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
    runner: Runner,
    history: History,
    spec: runner_pb2.SessionSpec,
    workspace: Path,
    failed_bridge_state: None,
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
        thread_id = await _open_thread_with_seed(http, model, spec, seed_prompt=seed_prompt, seed_answer=seed_answer)
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
            history,
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
            history,
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
    runner: Runner,
    history: History,
    spec: runner_pb2.SessionSpec,
    workspace: Path,
    failed_bridge_state: None,
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
        thread_id = await _open_thread_with_seed(http, model, spec, seed_prompt=seed_prompt, seed_answer=seed_answer)
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
            history,
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
        items, chunks = await _folded_items(history, thread_id)
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
                history,
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
    app_url: str, event_logs: SeededEventLogStore, history: History
) -> None:
    thread_id = await event_logs.seed(SANDBOX, "missing-runner-session")
    await history.open(thread_id, sandbox_name=SANDBOX)
    await register_gateway_session(history.engine, thread_id, sandbox=SANDBOX)
    async with httpx.AsyncClient(base_url=app_url, timeout=60, headers=AGENT_AUTH) as http:
        response = await http.post(f"/threads/{thread_id}/resume")
    assert response.status_code == 409


async def test_the_feed_records_a_turn_nobody_is_watching(
    app_url: str, model: ScriptedModel, spec: runner_pb2.SessionSpec
) -> None:
    """Opening a session starts its feed, so a turn driven over REST alone lands in the store."""
    async with httpx.AsyncClient(base_url=app_url, timeout=60, headers=AGENT_AUTH) as http:
        thread_id = await _create(http, spec, uuid4())
        accepted = await http.post(
            _commands(thread_id),
            json={"commandId": "input-1", "submitInput": {"text": "Reply with exactly: UNWATCHED_OK"}},
        )
        assert accepted.status_code == 200, accepted.text
        request = await model.request()
        assert request.user_texts[-1] == "Reply with exactly: UNWATCHED_OK"
        await model.reply(request, Text("UNWATCHED_OK"))
        (thread,) = (await http.get("/threads")).json()["threads"]
        stored = await _stored_events(http, thread["id"], until="turnCompleted")
        assert [entry["event"]["itemCompleted"]["text"] for entry in stored if "itemCompleted" in entry["event"]] == [
            "UNWATCHED_OK"
        ]
        assert (
            await http.post(_commands(thread_id), json={"commandId": "stop-unwatched", "stopRunnerSession": {}})
        ).status_code == 200
        assert (await http.get("/threads/00000000-0000-0000-0000-000000000000/events/stream")).status_code == 404


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
    app_url: str, local_runners: RunnerDirectory, spec: runner_pb2.SessionSpec, failed_bridge_state: None
) -> None:
    """An app prefix lag must still preserve the runner's id-conflict verdict as a 409."""
    async with httpx.AsyncClient(base_url=app_url, timeout=60, headers=AGENT_AUTH) as http:
        thread = await _create(http, spec, uuid4())
    original = command_pb2.Command(
        command_id="reused-before-copy", interrupt_turn=command_pb2.InterruptTurn(turn_id="first-target")
    )
    # Admitted through the Runner Gateway, not the app.
    await local_runners.gateway.command(local_runners.client(SANDBOX).destination, str(thread), original)

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
    runner: Runner,
    spec: runner_pb2.SessionSpec,
    monkeypatch: pytest.MonkeyPatch,
    failed_bridge_state: None,
) -> None:
    async with httpx.AsyncClient(base_url=app_url, timeout=60, headers=AGENT_AUTH) as http:
        thread_id = await _create(http, spec, uuid4())
        session = _runner_session(runner, thread_id)
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
            assert accepted.json()["admission"]["event"]["commandAdmitted"]["command"] == {
                "commandId": "stop-gated",
                "stopRunnerSession": {},
            }
            assert session.running
        finally:
            allow_shutdown.set()
            await asyncio.shield(response)


async def test_dispatcher_timeout_is_not_an_internal_server_error(
    app_url: str, spec: runner_pb2.SessionSpec, monkeypatch: pytest.MonkeyPatch, failed_bridge_state: None
) -> None:
    async with httpx.AsyncClient(base_url=app_url, timeout=60, headers=AGENT_AUTH) as http:
        thread_id = await _create(http, spec, uuid4())

        async def timed_out(
            _bridge: RunnerBridge, _thread_id: UUID, _command: command_pb2.Command, **_kwargs: object
        ) -> session_dispatcher_pb2.Dispatch:
            raise DispatcherRequestTimeoutError("timed-out-command")

        monkeypatch.setattr(RunnerBridge, "command", timed_out)
        response = await http.post(
            _commands(thread_id), json={"commandId": "timed-out-command", "submitInput": {"text": "not delivered"}}
        )
        assert response.status_code == 504, response.text
        assert "uncertain" in response.json()["detail"]


async def test_command_returns_dispatch_receipt_before_app_archive_catches_up(
    bridge: RunnerBridge,
    event_logs: SeededEventLogStore,
    content: ContentStore,
    history: History,
    live_index: LiveIndex,
    custom_objects: FakeCustomObjectsApi,
    core_v1: FakeCoreV1Api,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw, live_index.pods[SANDBOX] = seed_runner(custom_objects, core_v1, SANDBOX)
    live_index.sandboxes[SANDBOX] = raw
    thread = await event_logs.seed(SANDBOX, SESSION)
    await history.open(thread)
    await register_gateway_session(history.engine, thread, sandbox=SANDBOX)
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

    async def submit(
        destination: SandboxDestination, session_id: str, candidate: command_pb2.Command
    ) -> event_log_pb2.EventEntry:
        nonlocal called
        called += 1
        assert (destination.sandbox, session_id) == (SANDBOX, SESSION)
        assert candidate == command
        return receipt

    monkeypatch.setattr(bridge._runners.gateway, "command", submit)
    dispatch = await bridge.command(thread, command)
    assert isinstance(dispatch, session_dispatcher_pb2.Dispatch)
    assert dispatch.state == session_dispatcher_pb2.DELIVERY_STATE_RUNNER_ADMITTED
    assert dispatch.admission == receipt
    assert await content.admitted_command(thread, command) is None
    assert await event_logs.events(thread, limit=10) == []

    await history.record(thread, [receipt])
    assert await content.admitted_command(thread, command) == receipt
    # The archive, not the runner, answers exact retries once it has caught up.
    assert await bridge.command(thread, command) == receipt
    assert called == 1


async def test_dispatcher_command_path_queues_without_a_ready_runner_or_gateway_session_lookup(
    runners: RunnerDirectory, event_logs: SeededEventLogStore, content: ContentStore, history: History
) -> None:
    thread = await event_logs.seed(SANDBOX, SESSION)
    await history.open(thread)
    queued = session_dispatcher_pb2.Dispatch(
        command_id="queued-by-app",
        session_id=str(thread),
        sandbox=SANDBOX,
        state=session_dispatcher_pb2.DELIVERY_STATE_QUEUED_IN_DISPATCHER,
        reason="sandbox_resuming",
    )
    calls: list[tuple[str, str, command_pb2.Command]] = []

    class Dispatcher:
        async def command(
            self, *, sandbox: str, session_id: str, command: command_pb2.Command
        ) -> session_dispatcher_pb2.Dispatch:
            calls.append((sandbox, session_id, command))
            return queued

        async def get_dispatch(self, command_id: str) -> session_dispatcher_pb2.Dispatch:
            assert command_id == queued.command_id
            return queued

    dispatcher = Dispatcher()
    bridge = RunnerBridge(runners=runners, event_logs=event_logs, content=content, dispatcher=dispatcher)  # type: ignore[arg-type]
    command = command_pb2.Command(
        command_id=queued.command_id, submit_input=command_pb2.SubmitInput(text="wait while starting")
    )
    assert await bridge.command(thread, command, sandbox=SANDBOX) == queued
    assert calls == [(SANDBOX, str(thread), command)]
    assert await bridge.dispatch(thread, queued.command_id) == queued


async def test_dispatch_status_cannot_be_read_through_another_thread(
    runners: RunnerDirectory, event_logs: SeededEventLogStore, content: ContentStore, history: History
) -> None:
    thread = await event_logs.seed(SANDBOX, SESSION)
    await history.open(thread)
    leaked = session_dispatcher_pb2.Dispatch(
        command_id="owned-by-another-thread",
        session_id="another-session",
        sandbox=SANDBOX,
        state=session_dispatcher_pb2.DELIVERY_STATE_QUEUED_IN_DISPATCHER,
    )

    class Dispatcher:
        async def get_dispatch(self, _command_id: str) -> session_dispatcher_pb2.Dispatch:
            return leaked

    bridge = RunnerBridge(runners=runners, event_logs=event_logs, content=content, dispatcher=Dispatcher())  # type: ignore[arg-type]
    with pytest.raises(DispatchNotFoundError):
        await bridge.dispatch(thread, leaked.command_id)


@dataclass
class Replicas:
    owner: RunnerBridge
    # The owner replica's Session openings, which a test stops as the replica exiting would.
    owner_openings: asyncio.Task[None]
    survivor: RunnerBridge
    # What the survivor streams a thread from: its own pool and its own listener.
    survivor_event_logs: SeededEventLogStore
    survivor_changes: ChangeSignal


@pytest.fixture
async def replicas(
    gateway_endpoint: Endpoint,
    local_runners: RunnerDirectory,
    live_index: LiveIndex,
    runner: Runner,
    database_updates: WakeupListener,
    db_url: str,
    event_logs: SeededEventLogStore,
    content: ContentStore,
    history: History,
    history_client: HistoryServiceClient,
    history_token_file: Path,
) -> AsyncIterator[Replicas]:
    replica_engine = connect(db_url)
    survivor_event_logs = SeededEventLogStore(
        replica_engine,
        history=event_logs.history,
        gateway=event_logs.gateway,
        history_reader=history_client,
        summaries=Summaries(history_client),
    )
    owner = RunnerBridge(
        runners=local_runners,
        event_logs=event_logs,
        content=content,
        dispatcher=GatewayBackedDispatcher(local_runners, event_logs),
    )
    try:
        async with (
            # The survivor replica's own channel to the gateway.
            dial(gateway_endpoint.target) as survivor_channel,
            listening(replica_engine, history_client, history_token_file) as replica_updates,
            history_client.new_client() as owner_openings_client,
            history_client.new_client() as survivor_openings_client,
        ):
            survivor_runners = RunnerDirectory(live_index, gateway_endpoint.gateway(survivor_channel))
            survivor = RunnerBridge(
                runners=survivor_runners,
                event_logs=survivor_event_logs,
                content=ContentStore(replica_engine, history_reader=history_client),
                dispatcher=GatewayBackedDispatcher(survivor_runners, survivor_event_logs),
            )
            owner_openings = asyncio.create_task(
                SessionOpenings(owner_openings_client, event_logs, retry_interval_s=0.05).run()
            )
            survivor_openings = asyncio.create_task(
                SessionOpenings(survivor_openings_client, survivor_event_logs, retry_interval_s=0.05).run()
            )
            try:
                yield Replicas(
                    owner, owner_openings, survivor, survivor_event_logs, replica_updates.changes[Channel.THREADS]
                )
            finally:
                owner_openings.cancel()
                survivor_openings.cancel()
                await asyncio.gather(owner_openings, survivor_openings, return_exceptions=True)
    finally:
        await replica_engine.dispose()


async def frame_lines(frames: AsyncIterator[bytes]) -> AsyncIterator[str]:
    async for frame in frames:
        for line in frame.decode().splitlines():
            yield line


async def test_replica_commands_and_database_stream_survive_owner_replica_exit(
    replicas: Replicas, event_logs: SeededEventLogStore, model: ScriptedModel, spec: runner_pb2.SessionSpec
) -> None:
    thread = UUID((await replicas.owner.create_session(SANDBOX, uuid4(), spec)).session_id)
    async for attempt in AsyncRetrying(
        stop=stop_after_delay(10), wait=wait_fixed(0.05), retry=retry_if_exception_type(AssertionError)
    ):
        with attempt:
            assert await event_logs.feed_state(thread) is not None
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
        replicas.owner_openings.cancel()
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


async def test_history_openings_give_a_session_the_app_did_not_start_a_thread(
    gateway_endpoint: Endpoint,
    gateway: RunnerGatewayClient,
    custom_objects: FakeCustomObjectsApi,
    core_v1: FakeCoreV1Api,
    runner: Runner,
    store: ThreadStore,
    event_logs: SeededEventLogStore,
    spec: runner_pb2.SessionSpec,
    history_client: HistoryServiceClient,
) -> None:
    # The app's informer has never seen the Sandbox: the Thread comes from the History Service alone.
    raw, _ = seed_runner(custom_objects, core_v1, SANDBOX)
    destination = SandboxDestination(
        owner=ServiceAccount(namespace=raw["metadata"]["namespace"], name=SANDBOX),
        sandbox=SANDBOX,
        sandbox_uid=raw["metadata"]["uid"],
    )
    async with (
        dialing(runner, gateway_endpoint.control_target, runner_bearer(SANDBOX)),
        history_client.new_client() as openings_client,
    ):
        await asyncio.to_thread(gateway_endpoint.wait_connected, UUID(destination.sandbox_uid))
        openings = asyncio.create_task(SessionOpenings(openings_client, event_logs, retry_interval_s=0.05).run())
        try:
            session_id = uuid4()
            await gateway.runner(destination).create(session_id=session_id, spec=MessageToDict(spec))
            # Only the openings can create this row: no bridge exists, so no Open, command, or SSE request ran.
            async for attempt in AsyncRetrying(
                stop=stop_after_delay(10), wait=wait_fixed(0.1), retry=retry_if_exception_type(AssertionError)
            ):
                with attempt:
                    (thread,) = await store.list_threads()
                    assert thread.id == session_id
                    assert await event_logs.last_cursor(thread.id) > 0
            # The gateway still routes the Thread to its Sandbox.
            assert thread.sandbox == SANDBOX
        finally:
            openings.cancel()
            await asyncio.gather(openings, return_exceptions=True)


async def test_resumed_session_stream_does_not_end_at_previous_shutdown(
    replicas: Replicas, event_logs: SeededEventLogStore, model: ScriptedModel, spec: runner_pb2.SessionSpec
) -> None:
    thread = UUID((await replicas.owner.create_session(SANDBOX, uuid4(), spec)).session_id)
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
    await replicas.survivor.create_session(SANDBOX, thread, spec)
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
    local_runners: RunnerDirectory,
    live_index: LiveIndex,
    event_logs: SeededEventLogStore,
    database_updates: WakeupListener,
    spec: runner_pb2.SessionSpec,
    content: ContentStore,
    history: History,
) -> None:
    thread = UUID((await replicas.owner.create_session(SANDBOX, uuid4(), spec)).session_id)
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

    del live_index.sandboxes[SANDBOX], live_index.pods[SANDBOX]
    offline = RunnerBridge(
        runners=local_runners,
        event_logs=event_logs,
        content=content,
        dispatcher=GatewayBackedDispatcher(local_runners, event_logs),
    )
    # A lost HTTP response is retryable from the committed Thread prefix even after the
    # sandbox disappears: this answer must not attempt a new runner attachment, which with the
    # sandbox gone from the index would raise.
    assert isinstance(admitted, session_dispatcher_pb2.Dispatch)
    assert await offline.command(thread, stop) == admitted.admission
    async with (
        asyncio.timeout(10),
        aclosing(follow(event_logs, database_updates.changes[Channel.THREADS], thread, after_cursor=0)) as frames,
    ):
        lines = frame_lines(frames)
        assert (await next_message(lines)).event == "attached"
        assert await read_until(lines, "harnessExited") == stored
        assert (await next_message(lines)).event == "end"


async def test_open_and_resume_reply_before_archive_catches_up(
    local_runners: RunnerDirectory,
    event_logs: SeededEventLogStore,
    store: ThreadStore,
    content: ContentStore,
    model: ScriptedModel,
    spec: runner_pb2.SessionSpec,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Runner receipts and direct commands work while the Thread's History projection is pending."""
    # Stall the runner's push to the History Service's ingress without delaying the runner itself.
    monkeypatch.setattr("agentplane.runner.ingress.IngressPusher.push", AsyncMock())
    bridge = RunnerBridge(
        runners=local_runners,
        event_logs=event_logs,
        content=content,
        dispatcher=GatewayBackedDispatcher(local_runners, event_logs),
    )
    opened = await asyncio.wait_for(bridge.create_session(SANDBOX, uuid4(), spec), timeout=10)
    thread_id = UUID(opened.session_id)
    assert opened.attached.last_cursor > 0
    assert await event_logs.exists(thread_id)
    assert await store.get_thread(thread_id) is None
    assert await store.list_threads() == []
    assert await event_logs.last_cursor(thread_id) == 0

    # A native conversation must exist before Resume: an idle Open has no Claude/Codex
    # history to restore even if the runner subsequently reports STOPPED.
    seed = await bridge.command(
        thread_id,
        command_pb2.Command(command_id="seed-before-archive", submit_input=command_pb2.SubmitInput(text="SEED")),
    )
    assert isinstance(seed, session_dispatcher_pb2.Dispatch)
    assert seed.state == session_dispatcher_pb2.DELIVERY_STATE_RUNNER_ADMITTED
    await model.reply(await model.request(), Text("SEED_REPLY"))
    async for attempt in AsyncRetrying(
        stop=stop_after_delay(20), wait=wait_fixed(0.1), retry=retry_if_exception_type(AssertionError)
    ):
        with attempt:
            (summary,) = await local_runners.client(SANDBOX).list_sessions()
            assert summary.last_cursor > seed.admission.cursor
            assert not summary.active_turn_id
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
            assert summary.harness_state == runner_pb2.HARNESS_STATE_STOPPED
    resumed = await asyncio.wait_for(
        bridge.resume_thread(
            thread_id, expected_harness=Harness(runner_pb2.Harness.Name(spec.harness)), expected_cwd=spec.cwd
        ),
        timeout=10,
    )
    assert resumed.session_id == opened.session_id
    assert resumed.last_cursor > opened.attached.last_cursor
    assert await event_logs.last_cursor(thread_id) == 0


if __name__ == "__main__":
    pytest_bazel.main()
