"""The real Go app's Runner Gateway path under faults a deployment cannot be made to produce: a
harness killed or a runner restarted mid-turn, an app replica exiting, History's archive held back,
and a Session opened behind the app's back. The fault-free flows a browser sees are scenarios in
`agentplane/system_tests/scenarios/thread_stream.py`."""

from __future__ import annotations

import asyncio
import json
import logging
import shlex
import signal
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_bazel
from google.protobuf.json_format import MessageToDict, ParseDict
from sqlalchemy import select
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_delay, wait_fixed

from agentplane.app.testing.history import SessionHistory
from agentplane.app.testing.replication_process import AppProcess, GatewayAccess, SandboxesAccess, app_process
from agentplane.app.testing.runner_gateway import admitted, delivered_opening
from agentplane.history_service.client import HistoryServiceClient
from agentplane.history_service.fold.models import ThreadCheckpoint, ThreadEntity, ThreadPayloadChunk
from agentplane.history_service.testing.history import History
from agentplane.proto import event_pb2, session_pb2
from agentplane.env.e2e.testing.failure_journal import FailureEvidence
from agentplane.env.e2e.testing.process import RunningRunner, kill_harness
from agentplane.runner_gateway.client import SandboxControlClient, SandboxRunner
from agentplane.runner_gateway.dispatcher import session_dispatcher_pb2
from agentplane.runner_gateway.dispatcher.testing_client import SessionDispatcherClient
from agentplane.runner_gateway.testing.gateway_process import GatewayProcess
from agentplane.sandboxes.testing.kubernetes import SANDBOX, SandboxCluster
from agentplane.testing.scripted_model import ScriptedModel, ShellCall, Text

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# pytest loads this plugin by name from the test target's args.
# gazelle:include_dep //util/testing:pytest_logfile

SESSIONS = f"/sandboxes/{SANDBOX}/sessions"


@pytest.fixture
async def failed_bridge_state(
    request: pytest.FixtureRequest, runner: RunningRunner, event_logs: SessionHistory, sandbox_runner: SandboxRunner
) -> AsyncIterator[None]:
    """Preserve each service's cursor and admission state when an app-level bridge case fails.

    The `runner` fixture retains the harness-owned history and runner state on failure.
    """
    yield
    if (evidence := FailureEvidence.for_failed_call(request)) is None:
        return
    state: dict[str, Any] = {}
    for session in await sandbox_runner.list_sessions():
        session_id = session.session_id
        observed: dict[str, Any] = {"runner_cursor": session.last_cursor}
        state[session_id] = observed
        try:
            async with asyncio.timeout(5):
                thread_id = UUID(session_id)
                observed["history_cursor"] = await event_logs.last_cursor(thread_id)
                observed["history_watermark"] = await event_logs.read_watermark(thread_id)
                scope = await event_logs.current_scope(thread_id)
                observed["history_scope"] = (
                    None
                    if scope is None
                    else {"projection_epoch": scope.projection_epoch, "through_cursor": scope.through_cursor}
                )
                page = await event_logs.reader.read_session_events(session_id, after_cursor=0, limit=1)
                observed["history_cursor"] = page.last_cursor
                observed["history_feed"] = MessageToDict(page.feed_state) if page.HasField("feed_state") else None
        except Exception as error:
            observed["history_error"] = repr(error)
            logging.getLogger(__name__).warning("bridge failure history state capture failed", exc_info=True)
    evidence.write_json("bridge-state.json", state)


async def _create(http: httpx.AsyncClient, spec: session_pb2.SessionSpec, session_id: UUID, sandbox_uid: str) -> str:
    """Create a Session through the app, and wait for the Thread History's record of its opening gives it."""
    opened = await http.post(
        SESSIONS, json={"session_id": str(session_id), "sandbox_uid": sandbox_uid, "spec": MessageToDict(spec)}
    )
    assert opened.status_code == 201, opened.text
    thread_id = str(opened.json()["sessionId"])
    async for attempt in AsyncRetrying(
        stop=stop_after_delay(30), wait=wait_fixed(0.1), retry=retry_if_exception_type(AssertionError)
    ):
        with attempt:
            metadata = await http.get(f"/upstream/app/v1/threads/{thread_id}")
            summary = await http.get(f"/upstream/history/v1/sessions/{thread_id}/summary")
            assert metadata.status_code == 200, metadata.text
            assert summary.status_code == 200, summary.text
    return thread_id


async def _runner_session(runner: SandboxRunner, thread_id: str | UUID) -> session_pb2.SessionStatus:
    status = await runner.status()
    return next(session for session in status.sessions if session.session_id == str(thread_id))


async def _kill_harness(runner: SandboxRunner, thread_id: str | UUID) -> None:
    await kill_harness(await runner.status(), str(thread_id))


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
def sandbox_runner(
    sandbox: SandboxCluster, sandbox_control: SandboxControlClient, runner: RunningRunner
) -> SandboxRunner:
    """The production Runner Gateway's proxy for this test's Sandbox."""
    return sandbox_control.runner(sandbox.destination)


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
    http: httpx.AsyncClient,
    model: ScriptedModel,
    spec: session_pb2.SessionSpec,
    sandbox_uid: str,
    *,
    seed_prompt: str,
    seed_answer: str,
) -> str:
    thread_id = await _create(http, spec, uuid4(), sandbox_uid)
    accepted = await http.post(
        f"/upstream/dispatcher/v1/sessions/{thread_id}:submitInput",
        json={"sandbox": SANDBOX, "sandboxUid": sandbox_uid, "commandId": "seed-input", "text": seed_prompt},
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
    app: AppProcess,
    sandbox_runner: SandboxRunner,
    model: ScriptedModel,
    runner: RunningRunner,
    history: History,
    spec: session_pb2.SessionSpec,
    failed_bridge_state: None,
    stop_mode: str,
) -> None:
    seed_prompt, seed_answer = "Remember the seed prompt", "SEED_ANSWER_FOR_STREAM_RECOVERY"
    partial = "PARTIAL_STREAM_MUST_STAY_UNFINISHED"
    async with httpx.AsyncClient(
        base_url=app.url, timeout=60, cookies=app.cookies, headers={"Origin": app.url}
    ) as http:
        thread_id = await _open_thread_with_seed(
            http, model, spec, sandbox_runner.destination.sandbox_uid, seed_prompt=seed_prompt, seed_answer=seed_answer
        )
        accepted = await http.post(
            f"/upstream/dispatcher/v1/sessions/{thread_id}:submitInput",
            json={
                "sandbox": SANDBOX,
                "sandboxUid": sandbox_runner.destination.sandbox_uid,
                "commandId": "streaming-input",
                "text": "Start a long answer",
            },
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
        turn_id, before = await _interrupt_and_recover(http, runner, sandbox_runner, thread_id, stop_mode)
        accepted = await http.post(
            f"/upstream/dispatcher/v1/sessions/{thread_id}:submitInput",
            json={
                "sandbox": SANDBOX,
                "sandboxUid": sandbox_runner.destination.sandbox_uid,
                "commandId": "stream-recovery-input",
                "text": "Continue after recovery",
            },
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
    http: httpx.AsyncClient, runner: RunningRunner, sandbox_runner: SandboxRunner, thread_id: str, mode: str
) -> tuple[str, int]:
    session = await _runner_session(sandbox_runner, thread_id)
    turn_id = session.active_turn_id
    assert turn_id
    existing = await runner.history_entries(thread_id)
    before = existing[-1].cursor if existing else 0
    if mode == "kill":
        await _kill_harness(sandbox_runner, thread_id)
    elif mode == "interrupt":
        response = await http.post(
            f"/upstream/dispatcher/v1/sessions/{thread_id}:interruptTurn",
            json={
                "sandbox": SANDBOX,
                "sandboxUid": sandbox_runner.destination.sandbox_uid,
                "commandId": "interrupt-for-recovery",
                "turnId": turn_id,
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["state"] == "DELIVERY_STATE_RUNNER_ADMITTED"
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
    app: AppProcess,
    sandbox_runner: SandboxRunner,
    model: ScriptedModel,
    runner: RunningRunner,
    history: History,
    spec: session_pb2.SessionSpec,
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
    codex = spec.harness == session_pb2.HARNESS_CODEX
    expected = (
        {event_pb2.RECOVERY_DISPOSITION_UNKNOWN}
        if codex
        else {
            event_pb2.RECOVERY_DISPOSITION_RETAINED,
            event_pb2.RECOVERY_DISPOSITION_ABSENT,
            event_pb2.RECOVERY_DISPOSITION_REVISED,
        }
    )
    async with httpx.AsyncClient(
        base_url=app.url, timeout=60, cookies=app.cookies, headers={"Origin": app.url}
    ) as http:
        thread_id = await _open_thread_with_seed(
            http, model, spec, sandbox_runner.destination.sandbox_uid, seed_prompt=seed_prompt, seed_answer=seed_answer
        )
        accepted = await http.post(
            f"/upstream/dispatcher/v1/sessions/{thread_id}:submitInput",
            json={
                "sandbox": SANDBOX,
                "sandboxUid": sandbox_runner.destination.sandbox_uid,
                "commandId": "tool-input",
                "text": "Run the blocking side effect once",
            },
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
        turn_id, before = await _interrupt_and_recover(http, runner, sandbox_runner, thread_id, stop_mode)
        accepted = await http.post(
            f"/upstream/dispatcher/v1/sessions/{thread_id}:submitInput",
            json={
                "sandbox": SANDBOX,
                "sandboxUid": sandbox_runner.destination.sandbox_uid,
                "commandId": "tool-recovery-input",
                "text": recovery_prompt,
            },
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
    app: AppProcess,
    sandbox_runner: SandboxRunner,
    model: ScriptedModel,
    runner: RunningRunner,
    history: History,
    spec: session_pb2.SessionSpec,
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
    async with httpx.AsyncClient(
        base_url=app.url, timeout=60, cookies=app.cookies, headers={"Origin": app.url}
    ) as http:
        thread_id = await _open_thread_with_seed(
            http, model, spec, sandbox_runner.destination.sandbox_uid, seed_prompt=seed_prompt, seed_answer=seed_answer
        )
        accepted = await http.post(
            f"/upstream/dispatcher/v1/sessions/{thread_id}:submitInput",
            json={
                "sandbox": SANDBOX,
                "sandboxUid": sandbox_runner.destination.sandbox_uid,
                "commandId": "completed-tool-input",
                "text": "Run one completed tool",
            },
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

        turn_id, before = await _interrupt_and_recover(http, runner, sandbox_runner, thread_id, stop_mode)
        items, chunks = await _folded_items(history, thread_id)
        completed_tools = [
            row
            for row in items.values()
            if row.state["completion"] == "tool" and tool_output in _folded_body(chunks, row.output_ref)
        ]
        assert len(completed_tools) == 1
        assert completed_tools[0].state["tool_succeeded"] is True
        accepted = await http.post(
            f"/upstream/dispatcher/v1/sessions/{thread_id}:submitInput",
            json={
                "sandbox": SANDBOX,
                "sandboxUid": sandbox_runner.destination.sandbox_uid,
                "commandId": "completed-tool-recovery-input",
                "text": recovery_prompt,
            },
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


async def test_replica_commands_and_database_stream_survive_owner_replica_exit(
    app: AppProcess,
    db_url: str,
    history_client: HistoryServiceClient,
    sandbox: SandboxCluster,
    gateway_service: GatewayProcess,
    app_token_file: Path,
    http: httpx.AsyncClient,
    runner: RunningRunner,
    session_dispatcher: SessionDispatcherClient,
    model: ScriptedModel,
    spec: session_pb2.SessionSpec,
) -> None:
    sandboxes = SandboxesAccess(kubeconfig=sandbox.server.kubeconfig, namespace=sandbox.namespace)
    gateway = GatewayAccess(gateway_service.target, app_token_file)
    async with (
        app_process(db_url, history_client, sandboxes=sandboxes, gateway=gateway) as survivor,
        httpx.AsyncClient(
            base_url=survivor.url, cookies=survivor.cookies, headers={"Origin": survivor.url}, timeout=60
        ) as survivor_http,
    ):
        thread = UUID(await _create(http, spec, uuid4(), sandbox.uid))
        await delivered_opening(session_dispatcher, thread)
        async for attempt in AsyncRetrying(
            stop=stop_after_delay(10), wait=wait_fixed(0.05), retry=retry_if_exception_type(AssertionError)
        ):
            with attempt:
                response = await survivor_http.get(f"/upstream/app/v1/threads/{thread}")
                assert response.status_code == 200, response.text

        async with survivor_http.stream("GET", f"/threads/{thread}/events/stream") as response:
            assert response.status_code == 200, await response.aread()
            lines = response.aiter_lines()
            assert (await next_message(lines)).event == "attached"

            first_submit = await survivor_http.post(
                f"/upstream/dispatcher/v1/sessions/{thread}:submitInput",
                json={
                    "sandbox": SANDBOX,
                    "sandboxUid": sandbox.uid,
                    "commandId": "input-1",
                    "text": "FIRST_REPLICA_TURN",
                },
            )
            assert first_submit.status_code == 200, first_submit.text
            await model.reply(await model.request(), Text("FIRST_REPLICA_TURN"))
            async with asyncio.timeout(20):
                first = await read_until(lines, "turnCompleted")

            await app.kill()
            assert app.process.returncode == -signal.SIGKILL
            second_submit = await survivor_http.post(
                f"/upstream/dispatcher/v1/sessions/{thread}:submitInput",
                json={
                    "sandbox": SANDBOX,
                    "sandboxUid": sandbox.uid,
                    "commandId": "input-2",
                    "text": "AFTER_OWNER_EXIT",
                },
            )
            assert second_submit.status_code == 200, second_submit.text
            request = await model.request()
            await model.reply(request, Text("AFTER_OWNER_EXIT"))
            async with asyncio.timeout(20):
                second = await read_until(lines, "turnCompleted")
            seen = [*first, *second]
            cursors = [message.id for message in seen if message.id is not None]
            assert cursors == list(range(1, len(cursors) + 1))
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
    http: httpx.AsyncClient,
    spec: session_pb2.SessionSpec,
) -> None:
    # No app HTTP request opened this Thread; History's Session opening creates its row.
    session_id = uuid4()
    # This seeds a runner-owned Session without going through Dispatcher admission or app open.
    await sandbox_control.runner(sandbox.destination).open_session(session_id=session_id, spec=spec)
    # The app's production History watcher creates the Thread row; no command or SSE request ran.
    async for attempt in AsyncRetrying(
        stop=stop_after_delay(10), wait=wait_fixed(0.1), retry=retry_if_exception_type(AssertionError)
    ):
        with attempt:
            metadata = await http.get(f"/upstream/app/v1/threads/{session_id}")
            assert metadata.status_code == 200, metadata.text
            assert metadata.json()["threadId"] == str(session_id)
            summary = await http.get(f"/upstream/history/v1/sessions/{session_id}/summary")
            assert summary.status_code == 200, summary.text
            assert summary.json()["sandbox"]["name"] == SANDBOX


async def test_open_and_inputs_reach_the_runner_while_history_lags(
    sandbox: SandboxCluster,
    sandbox_runner: SandboxRunner,
    event_logs: SessionHistory,
    http: httpx.AsyncClient,
    model: ScriptedModel,
    spec: session_pb2.SessionSpec,
    session_dispatcher: SessionDispatcherClient,
    history: History,
) -> None:
    """The Dispatcher opens the Session and delivers its inputs, before and after a harness kill, while
    History's ingress is held back."""
    thread_id = UUID(await _create(http, spec, uuid4(), sandbox.uid))
    await delivered_opening(session_dispatcher, thread_id)
    await history.service.settled(thread_id, lambda _summary: True)
    (runner_session,) = [row for row in await sandbox_runner.list_sessions() if row.session_id == str(thread_id)]
    opened_cursor = runner_session.last_cursor
    assert opened_cursor == 0
    async with history.fence_ingress(thread_id):
        seed_response = await http.post(
            f"/upstream/dispatcher/v1/sessions/{thread_id}:submitInput",
            json={
                "sandbox": SANDBOX,
                "sandboxUid": sandbox_runner.destination.sandbox_uid,
                "commandId": "seed-before-archive",
                "text": "SEED",
            },
        )
        assert seed_response.status_code == 200, seed_response.text
        seed = await admitted(
            session_dispatcher, ParseDict(seed_response.json()["dispatch"], session_dispatcher_pb2.Dispatch())
        )
        assert await event_logs.read_watermark(thread_id) == 0
        await model.reply(await model.request(), Text("SEED_REPLY"))
        async for attempt in AsyncRetrying(
            stop=stop_after_delay(20), wait=wait_fixed(0.1), retry=retry_if_exception_type(AssertionError)
        ):
            with attempt:
                (summary,) = await sandbox_runner.list_sessions()
                assert summary.last_cursor > seed.admission.cursor
                assert not summary.active_turn_id

        await _kill_harness(sandbox_runner, thread_id)
        # The native process exited while History stayed behind; later input prepares it again.
        async for attempt in AsyncRetrying(
            stop=stop_after_delay(10), wait=wait_fixed(0.1), retry=retry_if_exception_type(AssertionError)
        ):
            with attempt:
                (summary,) = await sandbox_runner.list_sessions()
                assert summary.harness_state == session_pb2.HARNESS_STATE_STOPPED
        receipt_response = await http.post(
            f"/upstream/dispatcher/v1/sessions/{thread_id}:submitInput",
            json={
                "sandbox": SANDBOX,
                "sandboxUid": sandbox_runner.destination.sandbox_uid,
                "commandId": "after-archive-lag",
                "text": "CONTINUE",
            },
        )
        assert receipt_response.status_code == 200, receipt_response.text
        receipt = await admitted(
            session_dispatcher, ParseDict(receipt_response.json()["dispatch"], session_dispatcher_pb2.Dispatch())
        )
        assert receipt.admission.cursor > opened_cursor
        await model.reply(await model.request(), Text("CONTINUED"))
        control_cursor = 0
        async for attempt in AsyncRetrying(
            stop=stop_after_delay(20), wait=wait_fixed(0.1), retry=retry_if_exception_type(AssertionError)
        ):
            with attempt:
                (summary,) = await sandbox_runner.list_sessions()
                assert summary.last_cursor > receipt.admission.cursor
                assert not summary.active_turn_id
                control_cursor = summary.last_cursor
        # Runner progress is visible through the control stream while the ingress transaction
        # waits on the History row; the app archive remains behind until the lock is released.
        # This cursor is a controlled prefix, not a quiescence barrier: Claude can emit its native
        # command_lifecycle completed frame after the result has already completed the turn.
        assert await event_logs.last_cursor(thread_id) == 0

    async for attempt in AsyncRetrying(
        stop=stop_after_delay(20), wait=wait_fixed(0.1), retry=retry_if_exception_type(AssertionError)
    ):
        with attempt:
            (summary,) = await sandbox_runner.list_sessions()
            assert summary.last_cursor >= control_cursor
            assert await event_logs.read_watermark(thread_id) >= control_cursor
            assert await event_logs.last_cursor(thread_id) >= control_cursor
            archived = await event_logs.events(thread_id, limit=1000)
            assert seed.admission in archived
            assert receipt.admission in archived

            dispatches = (seed, receipt)
            confirmations = []
            for dispatch in dispatches:
                command_id = dispatch.admission.event.command_admitted.command.command_id
                matched = [
                    entry.event.harness_user_message_confirmed
                    for entry in archived
                    if entry.event.HasField("harness_user_message_confirmed")
                    and command_id in entry.event.harness_user_message_confirmed.origin_command_ids
                ]
                assert len(matched) == 1
                confirmations.extend(matched)

            completed_turn_ids = [
                entry.event.turn_completed.turn_id for entry in archived if entry.event.HasField("turn_completed")
            ]
            assert len(completed_turn_ids) == 2
            assert len(set(completed_turn_ids)) == 2
            assert {confirmation.turn_id for confirmation in confirmations} == set(completed_turn_ids)


if __name__ == "__main__":
    pytest_bazel.main()
