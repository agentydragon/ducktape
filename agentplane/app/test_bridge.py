"""The app's runner path under faults a deployment cannot be made to produce, for both harnesses: a
harness killed or a runner restarted mid-turn, an app replica exiting, History's archive held back,
a Dispatcher answering before delivery or ahead of the archive, and a Session opened behind the
app's back. The runner takes control through the Go Runner Gateway, whose Dispatcher delivers the
app's commands; the scripted Dispatchers stand in only where a case needs one to answer as no real
one would on cue. The fault-free flows a browser sees are scenarios in
`agentplane/system_tests/test_thread_stream.py`."""

from __future__ import annotations

import asyncio
import json
import logging
import shlex
from collections.abc import AsyncIterator, Callable
from contextlib import aclosing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast, override
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
from agentplane.app.conftest import listening
from agentplane.app.database import connect
from agentplane.app.database_updates import Channel
from agentplane.app.egress_access import EgressAccess
from agentplane.app.electric import ElectricProxy
from agentplane.app.identity import TokenReviewer
from agentplane.app.live import LiveIndex
from agentplane.app.notifications_operator_client import NotificationsOperatorClient
from agentplane.app.operator_sessions import OperatorSessionStore
from agentplane.app.sandbox_access import SandboxAccess
from agentplane.app.testing.dispatcher import served_dispatcher
from agentplane.app.testing.history import SeededEventLogStore
from agentplane.app.testing.model_test_data import TEST_MODELS
from agentplane.app.testing.runner_gateway import APP, admitted, delivered_opening, discover, session_dispatcher_client
from agentplane.app.testing.system_status import answering_system_status
from agentplane.app.threads.bridge import RunnerBridge
from agentplane.app.threads.dispatcher_client import SessionDispatcherClient
from agentplane.app.threads.events.stream import follow
from agentplane.app.threads.history_watch import SessionOpenings
from agentplane.app.threads.runner_directory import RunnerDirectory
from agentplane.app.threads.store import ThreadStore
from agentplane.app.threads.summaries import Summaries
from agentplane.app.threads.view.content import ContentStore
from agentplane.changes import ChangeSignal
from agentplane.egress.decisions_client import DecisionsClient
from agentplane.grpc.calls import dial
from agentplane.grpc.serving import serving
from agentplane.history_service.client import HistoryServiceClient
from agentplane.history_service.fold.models import ThreadCheckpoint, ThreadEntity, ThreadPayloadChunk
from agentplane.history_service.testing.history import History
from agentplane.postgres.wakeups import WakeupListener
from agentplane.proto import command_pb2, event_log_pb2, event_pb2, runner_pb2
from agentplane.runner import runner_service_pb2
from agentplane.runner.testing.failure_journal import FailureEvidence
from agentplane.runner.testing.process import RunningRunner, kill_harness
from agentplane.runner_gateway.client import SandboxControlClient
from agentplane.runner_gateway.dispatcher import session_dispatcher_pb2, session_dispatcher_pb2_grpc
from agentplane.runner_gateway.testing.gateway_process import GatewayProcess
from agentplane.sandboxes.testing.kubernetes import SANDBOX, SandboxCluster
from agentplane.testing.scripted_model import ScriptedModel, ShellCall, Text
from util.testing.asgi import serve_app_in_loop

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# pytest loads this plugin by name from the test target's args.
# gazelle:include_dep //util/testing:pytest_logfile

SESSION = "8e414bdd-1310-4687-b768-3f1d69047122"
SESSIONS = f"/sandboxes/{SANDBOX}/sessions"


@pytest.fixture
async def failed_bridge_state(
    request: pytest.FixtureRequest,
    runner: RunningRunner,
    event_logs: SeededEventLogStore,
    history_client: HistoryServiceClient,
    local_runners: RunnerDirectory,
) -> AsyncIterator[None]:
    """Preserve each service's cursor and admission state when an app-level bridge case fails.

    The `runner` fixture retains the harness-owned history and runner state on failure.
    """
    yield
    if (evidence := FailureEvidence.for_failed_call(request)) is None:
        return
    state: dict[str, Any] = {}
    for session in await local_runners.client(SANDBOX).list_sessions():
        session_id = session.session_id
        observed: dict[str, Any] = {"runner_cursor": session.last_cursor}
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
    """Create a Session through the app, and wait for the Thread History's record of its opening gives it."""
    opened = await http.post(SESSIONS, json={"session_id": str(session_id), "spec": MessageToDict(spec)})
    assert opened.status_code == 201, opened.text
    thread_id = str(opened.json()["sessionId"])
    async for attempt in AsyncRetrying(
        stop=stop_after_delay(30), wait=wait_fixed(0.1), retry=retry_if_exception_type(AssertionError)
    ):
        with attempt:
            assert (await http.get(f"/threads/{thread_id}")).status_code == 200
    return thread_id


async def _submit_input(
    bridge: RunnerBridge, thread_id: UUID, sandbox_uid: str, command_id: str, text: str
) -> session_dispatcher_pb2.Dispatch:
    response = cast(
        session_dispatcher_pb2.SubmitInputResponse,
        await bridge.dispatcher.unary(
            bridge.dispatcher.stub.SubmitInput,
            session_dispatcher_pb2.SubmitInputRequest(
                command_id=command_id, session_id=str(thread_id), sandbox=SANDBOX, sandbox_uid=sandbox_uid, text=text
            ),
        ),
    )
    return response.dispatch


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
        bridge.dispatcher = SessionDispatcherClient(channel, token_file=token_file, request_timeout_s=5)

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


async def _runner_session(runners: RunnerDirectory, thread_id: str | UUID) -> runner_pb2.SessionStatus:
    status = await runners.client(SANDBOX).status()
    return next(session for session in status.sessions if session.session_id == str(thread_id))


async def _kill_harness(runners: RunnerDirectory, thread_id: str | UUID) -> None:
    await kill_harness(await runners.client(SANDBOX).status(), str(thread_id))


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
async def local_runners(
    live_index: LiveIndex, sandbox: SandboxCluster, sandbox_control: SandboxControlClient, runner: RunningRunner
) -> RunnerDirectory:
    """`SANDBOX` in the app's index, its runner connected to the Gateway."""
    await discover(live_index, sandbox.server, namespace=sandbox.namespace, name=sandbox.name)
    return RunnerDirectory(live_index, sandbox_control)


@pytest.fixture
def inventory(sandbox: SandboxCluster) -> SandboxAccess:
    return SandboxAccess(sandbox.inventory, default_policy_sets=frozenset(), caller=APP)


@pytest.fixture
async def session_openings(
    history_client: HistoryServiceClient, event_logs: SeededEventLogStore
) -> AsyncIterator[None]:
    """A Thread for every Session History records, as `main.py` runs it."""
    async with history_client.new_client() as client:
        openings = asyncio.create_task(SessionOpenings(client, event_logs, retry_interval_s=0.05).run())
        try:
            yield
        finally:
            openings.cancel()
            await asyncio.gather(openings, return_exceptions=True)


@pytest.fixture
async def app_url(
    local_runners: RunnerDirectory,
    session_openings: None,
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
    notifications: NotificationsOperatorClient,
    session_dispatcher: SessionDispatcherClient,
) -> AsyncIterator[str]:
    """The app served by uvicorn, with the one test Sandbox's runner behind the Gateway. The server
    is real because SSE needs a response that streams, which an in-process ASGI transport would
    buffer."""
    bridge = RunnerBridge(runners=local_runners, event_logs=event_logs, content=content, dispatcher=session_dispatcher)
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
        notifications=notifications,
        system_status=answering_system_status(),
    )
    async with serve_app_in_loop(app) as origin:
        yield origin


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
        f"/threads/{thread_id}/submit-input", json={"commandId": "seed-input", "text": seed_prompt}
    )
    assert accepted.status_code == 200, accepted.text
    request = await _next_model_request(model)
    assert request.user_texts[-1] == seed_prompt
    await model.reply(request, Text(seed_answer))
    await _stored_events(http, thread_id, until="turnCompleted")
    return thread_id


def _has_turn_status(entries: list[dict[str, Any]], status: str) -> bool:
    return any(entry["event"].get("turnCompleted", {}).get("status") == status for entry in entries)


@pytest.mark.parametrize("stop_mode", ["interrupt", "shutdown", "kill"])
async def test_stream_recovery_reports_the_content_the_model_receives(
    app_url: str,
    agent_auth: dict[str, str],
    local_runners: RunnerDirectory,
    model: ScriptedModel,
    runner: RunningRunner,
    history: History,
    spec: runner_pb2.SessionSpec,
    failed_bridge_state: None,
    stop_mode: str,
) -> None:
    seed_prompt, seed_answer = "Remember the seed prompt", "SEED_ANSWER_FOR_STREAM_RECOVERY"
    partial = "PARTIAL_STREAM_MUST_STAY_UNFINISHED"
    async with httpx.AsyncClient(base_url=app_url, timeout=60, headers=agent_auth) as http:
        thread_id = await _open_thread_with_seed(http, model, spec, seed_prompt=seed_prompt, seed_answer=seed_answer)
        accepted = await http.post(
            f"/threads/{thread_id}/submit-input", json={"commandId": "streaming-input", "text": "Start a long answer"}
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
        turn_id, before = await _interrupt_and_recover(http, runner, local_runners, thread_id, stop_mode)
        accepted = await http.post(
            f"/threads/{thread_id}/submit-input",
            json={"commandId": "stream-recovery-input", "text": "Continue after recovery"},
        )
        assert accepted.status_code == 200, accepted.text
        request = await _next_model_request(model)
        recovery_cursor = await _wait_for_conversation_reconciliation(http, runner, thread_id, turn_id, before)
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


async def _interrupt_and_recover(
    http: httpx.AsyncClient, runner: RunningRunner, runners: RunnerDirectory, thread_id: str, mode: str
) -> tuple[str, int]:
    session = await _runner_session(runners, thread_id)
    turn_id = session.active_turn_id
    assert turn_id
    existing = await runner.history_entries(thread_id)
    before = existing[-1].cursor if existing else 0
    if mode == "kill":
        await _kill_harness(runners, thread_id)
    elif mode == "interrupt":
        response = await http.post(
            f"/threads/{thread_id}/interrupt-turn", json={"commandId": "interrupt-for-recovery", "turnId": turn_id}
        )
        assert response.status_code == 200, response.text
        assert response.json()["kind"] == "interrupt_result"
        assert response.json()["result"]["state"] == "DELIVERY_STATE_RUNNER_ADMITTED"
    else:
        await runner.restart()
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

    return turn_id, before


async def _wait_for_conversation_reconciliation(
    http: httpx.AsyncClient, runner: RunningRunner, thread_id: str, turn_id: str, before: int
) -> int:
    async for attempt in AsyncRetrying(
        stop=stop_after_delay(10), wait=wait_fixed(0.05), retry=retry_if_exception_type(AssertionError)
    ):
        with attempt:
            reports = [
                entry
                for entry in await runner.history_entries(thread_id, after=before)
                if entry.event.HasField("conversation_reconciled")
                and entry.event.conversation_reconciled.turn_id == turn_id
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
    agent_auth: dict[str, str],
    local_runners: RunnerDirectory,
    model: ScriptedModel,
    runner: RunningRunner,
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
    codex = spec.harness == runner_pb2.HARNESS_CODEX
    expected = (
        {event_pb2.RECOVERY_DISPOSITION_UNKNOWN}
        if codex
        else {
            event_pb2.RECOVERY_DISPOSITION_RETAINED,
            event_pb2.RECOVERY_DISPOSITION_ABSENT,
            event_pb2.RECOVERY_DISPOSITION_REVISED,
        }
    )
    async with httpx.AsyncClient(base_url=app_url, timeout=60, headers=agent_auth) as http:
        thread_id = await _open_thread_with_seed(http, model, spec, seed_prompt=seed_prompt, seed_answer=seed_answer)
        accepted = await http.post(
            f"/threads/{thread_id}/submit-input",
            json={"commandId": "tool-input", "text": "Run the blocking side effect once"},
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
        turn_id, before = await _interrupt_and_recover(http, runner, local_runners, thread_id, stop_mode)
        accepted = await http.post(
            f"/threads/{thread_id}/submit-input", json={"commandId": "tool-recovery-input", "text": recovery_prompt}
        )
        assert accepted.status_code == 200, accepted.text
        request = await _next_model_request(model)
        recovery_cursor = await _wait_for_conversation_reconciliation(http, runner, thread_id, turn_id, before)
        items, chunks = await _wait_for_folded_items(
            history,
            thread_id,
            lambda rows, payloads: (
                rows[item_id].revision_cursor >= recovery_cursor and rows[item_id].state["recovery"] in expected
            ),
        )
        recovered = items[item_id]
        assert recovered.state["tool_succeeded"] is not True
        if stop_mode == "kill":
            assert recovered.state["completion"] is None
        assert marker.read_text().splitlines() == [token]
        assert seed_prompt in request.user_texts
        assert seed_answer in request.assistant_texts
        assert ("inflight-tool" in request.tool_calls) == (
            recovered.state["recovery"] != event_pb2.RECOVERY_DISPOSITION_ABSENT
        )
        recovered_output = _folded_body(chunks, recovered.output_ref)
        # Codex's display history cannot establish the synthetic model output of an
        # interrupted command. Preserve the observed output without presenting it as retained.
        if not codex and recovered_output and recovered.state["recovery"] != event_pb2.RECOVERY_DISPOSITION_ABSENT:
            assert any(output.contains(recovered_output) for output in request.tool_outputs)
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
    agent_auth: dict[str, str],
    local_runners: RunnerDirectory,
    model: ScriptedModel,
    runner: RunningRunner,
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
    async with httpx.AsyncClient(base_url=app_url, timeout=60, headers=agent_auth) as http:
        thread_id = await _open_thread_with_seed(http, model, spec, seed_prompt=seed_prompt, seed_answer=seed_answer)
        accepted = await http.post(
            f"/threads/{thread_id}/submit-input",
            json={"commandId": "completed-tool-input", "text": "Run one completed tool"},
        )
        assert accepted.status_code == 200, accepted.text
        request = await _next_model_request(model)
        await model.reply(request, ShellCall("completed-tool", command))
        continuation = await _next_model_request(model)
        assert any(result.contains(tool_output) for result in continuation.tool_outputs)
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

        turn_id, before = await _interrupt_and_recover(http, runner, local_runners, thread_id, stop_mode)
        items, chunks = await _folded_items(history, thread_id)
        completed_tools = [
            row
            for row in items.values()
            if row.state["completion"] == "tool" and tool_output in _folded_body(chunks, row.output_ref)
        ]
        assert len(completed_tools) == 1
        assert completed_tools[0].state["tool_succeeded"] is True
        accepted = await http.post(
            f"/threads/{thread_id}/submit-input",
            json={"commandId": "completed-tool-recovery-input", "text": recovery_prompt},
        )
        assert accepted.status_code == 200, accepted.text
        request = await _next_model_request(model)
        recovery_cursor = await _wait_for_conversation_reconciliation(http, runner, thread_id, turn_id, before)
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
            assert any(result.contains(tool_output) for result in request.tool_outputs) == (
                recovered_tool.state["recovery"] == event_pb2.RECOVERY_DISPOSITION_RETAINED
            )
            assert marker.read_text().splitlines() == [token]
            await exchange.send(*model.stream(request, [Text("COMPLETED_TOOL_RECOVERY_DONE")]))
        await _stored_events(
            http,
            thread_id,
            until="turnCompleted",
            matches=lambda entries: _has_turn_status(entries, "TURN_STATUS_COMPLETED"),
        )
        assert marker.read_text().splitlines() == [token]


async def test_command_returns_dispatch_receipt_before_app_archive_catches_up(
    sandbox: SandboxCluster,
    bridge: RunnerBridge,
    event_logs: SeededEventLogStore,
    content: ContentStore,
    history: History,
    tmp_path: Path,
) -> None:
    thread = await event_logs.seed(SANDBOX, SESSION)
    await history.open(thread)
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
    received: list[session_dispatcher_pb2.InterruptTurnRequest] = []

    class AdmittedInterrupt(session_dispatcher_pb2_grpc.SessionDispatcherServicer):
        @override
        async def InterruptTurn(self, request, _context):
            received.append(request)
            return session_dispatcher_pb2.InterruptTurnResponse(
                state=session_dispatcher_pb2.DELIVERY_STATE_RUNNER_ADMITTED, admission=receipt
            )

    async with served_dispatcher(
        AdmittedInterrupt(), tmp_path / "admitted-dispatcher-token", request_timeout_s=5
    ) as client:
        bridge.dispatcher = client
        response = await bridge.dispatcher.unary(
            bridge.dispatcher.stub.InterruptTurn,
            session_dispatcher_pb2.InterruptTurnRequest(
                command_id=command.command_id,
                session_id=str(thread),
                sandbox=SANDBOX,
                sandbox_uid=sandbox.uid,
                turn_id=command.interrupt_turn.turn_id,
            ),
        )
        assert isinstance(response, session_dispatcher_pb2.InterruptTurnResponse)
        assert response.state == session_dispatcher_pb2.DELIVERY_STATE_RUNNER_ADMITTED
        assert response.admission == receipt
        assert await content.admitted_command(thread, command) is None
        assert await event_logs.events(thread, limit=10) == []

        await history.record(thread, [receipt])
        assert await content.admitted_command(thread, command) == receipt
        # The archive, not the runner, answers exact retries once it has caught up.
        assert await content.admitted_command(thread, command) == receipt
    assert [(request.sandbox, request.session_id, request.command_id, request.turn_id) for request in received] == [
        (SANDBOX, str(thread), command.command_id, command.interrupt_turn.turn_id)
    ]


@dataclass
class Replicas:
    owner: RunnerBridge
    # The owner replica's Session openings, which a test stops as the replica exiting would.
    owner_openings: asyncio.Task[None]
    survivor: RunnerBridge
    # What the survivor streams a thread from: its own pool and its own listener.
    survivor_event_logs: SeededEventLogStore
    survivor_changes: ChangeSignal
    sandbox_uid: str


@pytest.fixture
async def replicas(
    gateway_service: GatewayProcess,
    app_token_file: Path,
    local_runners: RunnerDirectory,
    live_index: LiveIndex,
    database_updates: WakeupListener,
    db_url: str,
    event_logs: SeededEventLogStore,
    content: ContentStore,
    history: History,
    history_client: HistoryServiceClient,
    session_dispatcher: SessionDispatcherClient,
    sandbox: SandboxCluster,
) -> AsyncIterator[Replicas]:
    replica_engine = connect(db_url)
    survivor_event_logs = SeededEventLogStore(
        replica_engine,
        history=event_logs.history,
        history_reader=history_client,
        summaries=Summaries(history_client),
        sandbox_namespaces=event_logs.sandbox_namespaces,
    )
    owner = RunnerBridge(runners=local_runners, event_logs=event_logs, content=content, dispatcher=session_dispatcher)
    try:
        async with (
            listening(replica_engine, history_client) as replica_updates,
            history_client.new_client() as owner_openings_client,
            history_client.new_client() as survivor_openings_client,
        ):
            survivor_runners = RunnerDirectory(live_index, gateway_service.gateway_client(app_token_file))
            async with session_dispatcher_client(gateway_service, app_token_file) as survivor_dispatcher:
                survivor = RunnerBridge(
                    runners=survivor_runners,
                    event_logs=survivor_event_logs,
                    content=ContentStore(replica_engine, history_reader=history_client),
                    dispatcher=survivor_dispatcher,
                )
                owner_openings = asyncio.create_task(
                    SessionOpenings(owner_openings_client, event_logs, retry_interval_s=0.05).run()
                )
                survivor_openings = asyncio.create_task(
                    SessionOpenings(survivor_openings_client, survivor_event_logs, retry_interval_s=0.05).run()
                )
                try:
                    yield Replicas(
                        owner,
                        owner_openings,
                        survivor,
                        survivor_event_logs,
                        replica_updates.changes[Channel.THREADS],
                        sandbox.uid,
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
    runner: RunningRunner,
    replicas: Replicas,
    event_logs: SeededEventLogStore,
    model: ScriptedModel,
    spec: runner_pb2.SessionSpec,
) -> None:
    thread = UUID((await replicas.owner.create_session(SANDBOX, uuid4(), spec)).session_id)
    async for attempt in AsyncRetrying(
        stop=stop_after_delay(10), wait=wait_fixed(0.05), retry=retry_if_exception_type(AssertionError)
    ):
        with attempt:
            # The Thread row comes from the Session openings once History records the opening.
            assert await replicas.survivor_event_logs.exists(thread)
            assert await event_logs.feed_state(thread) is not None
    async with aclosing(
        follow(replicas.survivor_event_logs, replicas.survivor_changes, thread, after_cursor=0)
    ) as frames:
        lines = frame_lines(frames)
        assert (await next_message(lines)).event == "attached"
        await _submit_input(replicas.survivor, thread, replicas.sandbox_uid, "input-1", "FIRST_REPLICA_TURN")
        await model.reply(await model.request(), Text("FIRST_REPLICA_TURN"))
        async with asyncio.timeout(10):
            first = await read_until(lines, "turnCompleted")

        await _submit_input(replicas.survivor, thread, replicas.sandbox_uid, "input-2", "AFTER_OWNER_EXIT")
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
        await runner.restart()
        async with asyncio.timeout(10):
            await read_until(lines, "harnessExited")
            assert (await next_message(lines)).event == "end"


async def test_history_openings_give_a_session_the_app_did_not_start_a_thread(
    sandbox: SandboxCluster,
    sandbox_control: SandboxControlClient,
    runner: RunningRunner,
    store: ThreadStore,
    spec: runner_pb2.SessionSpec,
    session_openings: None,
) -> None:
    # The app's informer has never seen the Sandbox: the Thread comes from the History Service alone.
    session_id = uuid4()
    # This seeds a runner-owned Session without going through Dispatcher admission or app open.
    await sandbox_control.runner_unary(
        sandbox.destination,
        lambda stub: stub.OpenSession,
        runner_service_pb2.OpenSessionRequest(session_id=str(session_id), spec=spec),
        timeout_s=sandbox_control.lifecycle_timeout_s,
    )
    # Only the openings can create this row: no bridge exists, so no command or SSE request ran.
    async for attempt in AsyncRetrying(
        stop=stop_after_delay(10), wait=wait_fixed(0.1), retry=retry_if_exception_type(AssertionError)
    ):
        with attempt:
            rows = await store.list_threads()
            assert len(rows) == 1
            thread = rows[0]
            assert thread.id == session_id
    assert thread.sandbox == SANDBOX


async def test_open_and_inputs_reach_the_runner_while_history_lags(
    sandbox: SandboxCluster,
    local_runners: RunnerDirectory,
    event_logs: SeededEventLogStore,
    content: ContentStore,
    model: ScriptedModel,
    spec: runner_pb2.SessionSpec,
    session_dispatcher: SessionDispatcherClient,
    history: History,
) -> None:
    """The Dispatcher opens the Session and delivers its inputs, before and after a harness kill, while
    History's ingress is held back."""
    bridge = RunnerBridge(runners=local_runners, event_logs=event_logs, content=content, dispatcher=session_dispatcher)
    opened = await bridge.create_session(SANDBOX, uuid4(), spec)
    thread_id = UUID(opened.session_id)
    await delivered_opening(session_dispatcher, thread_id)
    await history.service.settled(thread_id, lambda _summary: True)
    (runner_session,) = [
        row for row in await local_runners.client(SANDBOX).list_sessions() if row.session_id == opened.session_id
    ]
    opened_cursor = runner_session.last_cursor
    assert opened_cursor == 0
    async with history.fence_ingress(thread_id):
        seed = await admitted(
            session_dispatcher, await _submit_input(bridge, thread_id, sandbox.uid, "seed-before-archive", "SEED")
        )
        await model.reply(await model.request(), Text("SEED_REPLY"))
        async for attempt in AsyncRetrying(
            stop=stop_after_delay(20), wait=wait_fixed(0.1), retry=retry_if_exception_type(AssertionError)
        ):
            with attempt:
                (summary,) = await local_runners.client(SANDBOX).list_sessions()
                assert summary.last_cursor > seed.admission.cursor
                assert not summary.active_turn_id

        await _kill_harness(local_runners, thread_id)
        # The native process exited while History stayed behind; later input prepares it again.
        async for attempt in AsyncRetrying(
            stop=stop_after_delay(10), wait=wait_fixed(0.1), retry=retry_if_exception_type(AssertionError)
        ):
            with attempt:
                (summary,) = await local_runners.client(SANDBOX).list_sessions()
                assert summary.harness_state == runner_pb2.HARNESS_STATE_STOPPED
        receipt = await admitted(
            session_dispatcher, await _submit_input(bridge, thread_id, sandbox.uid, "after-archive-lag", "CONTINUE")
        )
        assert receipt.admission.cursor > opened_cursor
        await model.reply(await model.request(), Text("CONTINUED"))
        control_cursor = 0
        async for attempt in AsyncRetrying(
            stop=stop_after_delay(20), wait=wait_fixed(0.1), retry=retry_if_exception_type(AssertionError)
        ):
            with attempt:
                (summary,) = await local_runners.client(SANDBOX).list_sessions()
                assert summary.last_cursor > receipt.admission.cursor
                assert not summary.active_turn_id
                control_cursor = summary.last_cursor
        # Runner progress is visible through the control stream while the ingress transaction
        # waits on the History row; the app archive remains behind until the lock is released.
        assert await event_logs.last_cursor(thread_id) == 0

    async for attempt in AsyncRetrying(
        stop=stop_after_delay(20), wait=wait_fixed(0.1), retry=retry_if_exception_type(AssertionError)
    ):
        with attempt:
            (summary,) = await local_runners.client(SANDBOX).list_sessions()
            assert summary.last_cursor == control_cursor
            assert await event_logs.read_watermark(thread_id) == control_cursor
            assert await event_logs.last_cursor(thread_id) == control_cursor
            archived = await event_logs.events(thread_id, limit=1000)
            assert seed.admission in archived
            assert receipt.admission in archived
            assert len([entry for entry in archived if entry.event.HasField("turn_completed")]) == 2


if __name__ == "__main__":
    pytest_bazel.main()
