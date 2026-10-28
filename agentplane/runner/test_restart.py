"""A runner process dies with its harnesses; the next one on the same state directory carries on."""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import shlex
import shutil
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any

import pytest
import pytest_bazel

from agentplane.history_service.testing.go_service import GoHistoryService
from agentplane.native.codex.testing import model_catalog
from agentplane.native.codex.testing.responses import OpenAIResponses
from agentplane.proto import event_pb2, runner_pb2
from agentplane.runner.testing import events
from agentplane.runner.testing.driver import RunnerDriver, runner_test_session_id
from agentplane.runner.testing.gateway import Gateway
from agentplane.runner.testing.process_fixtures import RunnerProcess
from agentplane.testing.scripted_model import Reasoning, ScriptedModel, ShellCall, Text

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf


async def _wait_for_session_ready(driver: RunnerDriver, session_id: str) -> None:
    """Wait for startup recovery before attaching a follower with a status snapshot."""
    session_id = runner_test_session_id(session_id)
    runner = await driver.gateway.control.runner()
    async with asyncio.timeout(60):
        while True:
            status = await runner.GetStatus(runner_pb2.GetStatusRequest())
            session = next((row for row in status.sessions if row.session_id == session_id), None)
            if session is not None:
                assert session.execution_state != runner_pb2.SESSION_EXECUTION_STATE_FAILED, session
                if session.execution_state == runner_pb2.SESSION_EXECUTION_STATE_READY:
                    return
            await asyncio.sleep(0.01)


def _native_frames(entries: Sequence[Any], direction: int) -> list[dict[str, Any]]:
    return [
        json.loads(entry.event.native.line)
        for entry in events.of_kind(entries, "native")
        if entry.event.native.direction == direction
    ]


def _codex_history_items(
    entries: Sequence[Any], *, recovery_turn_ids: set[str], require_resume: bool = False
) -> dict[str, list[dict[str, Any]]]:
    """Read the item pages recorded on the runner's public native-frame archive."""
    requests = _native_frames(entries, event_pb2.DIRECTION_TO_HARNESS)
    responses = _native_frames(entries, event_pb2.DIRECTION_FROM_HARNESS)
    responses_by_id = {frame["id"]: frame for frame in responses if "id" in frame}

    handshake_frames = [frame for frame in requests if frame.get("method") in {"thread/start", "thread/resume"}]
    assert handshake_frames
    assert all(frame["id"] in responses_by_id for frame in handshake_frames)
    assert all(
        responses_by_id[frame["id"]]["result"]["thread"]["historyMode"] == "paginated" for frame in handshake_frames
    )
    thread_ids = {responses_by_id[frame["id"]]["result"]["thread"]["id"] for frame in handshake_frames}
    assert len(thread_ids) == 1, thread_ids
    (thread_id,) = thread_ids
    resume_frames = [frame for frame in handshake_frames if frame["method"] == "thread/resume"]
    if require_resume:
        assert resume_frames
        assert all(frame["params"].get("excludeTurns") is True for frame in resume_frames)

    assert not [frame for frame in requests if frame.get("method") == "thread/read"]
    assert not [frame for frame in requests if frame.get("method") in {"thread/turns/list", "thread/timeline/list"}]
    page_requests = [frame for frame in requests if frame.get("method") == "thread/items/list"]
    assert page_requests

    pages_by_turn: dict[str, list[dict[str, Any]]] = {}
    next_cursor_by_turn: dict[str, str | None] = {}
    for frame in page_requests:
        params = frame["params"]
        turn_id = params.get("turnId")
        assert turn_id in recovery_turn_ids, frame
        assert params.get("threadId") == thread_id, frame
        assert params.get("limit") == 100, frame
        assert params.get("cursor") == next_cursor_by_turn.get(turn_id), frame
        assert frame["id"] in responses_by_id, frame
        response = responses_by_id[frame["id"]]
        assert "error" not in response, response
        result = response["result"]
        assert all(row["turnId"] == turn_id for row in result["data"]), result
        pages_by_turn.setdefault(turn_id, []).extend(row["item"] for row in result["data"])
        next_cursor_by_turn[turn_id] = result.get("nextCursor")

    assert all(cursor is None for cursor in next_cursor_by_turn.values()), next_cursor_by_turn
    return pages_by_turn


async def test_runner_shutdown_waits_for_setup_to_finish(
    spec: runner_pb2.SessionSpec,
    start_runner: Callable[..., Awaitable[RunnerProcess]],
    driver: RunnerDriver,
    tmp_path: Path,
) -> None:
    started, continue_setup, completed = (
        tmp_path / name for name in ("setup-started", "setup-continue", "setup-completed")
    )
    script = (
        f"printf started > {shlex.quote(str(started))}\n"
        'printf "started\\n"\n'
        f"while [ ! -f {shlex.quote(str(continue_setup))} ]; do sleep 0.01; done\n"
        f"printf completed > {shlex.quote(str(completed))}\n"
    )
    first_runner = await start_runner()
    first = await driver.attach("setup-graceful-shutdown", spec=spec, setup_script=script)
    await first.until(events.is_kind("setup_output"))
    stopping = asyncio.create_task(first_runner.stop())
    try:
        await asyncio.sleep(0.05)
        assert not stopping.done()
        assert not completed.exists()
    finally:
        continue_setup.touch()
        await asyncio.gather(stopping, return_exceptions=True)

    await start_runner()
    second = await driver.attach("setup-graceful-shutdown")
    await second.until(events.is_kind("setup_finished"))
    assert second.attached.setup_state == runner_pb2.SETUP_STATE_SUCCEEDED
    assert len(events.of_kind(second.seen, "setup_started")) == 1
    assert len(events.of_kind(second.seen, "setup_finished")) == 1
    assert not events.of_kind(second.seen, "setup_interrupted")
    assert not events.of_kind(second.seen, "harness_started")
    assert completed.exists()


async def test_runner_kill_interrupts_setup_without_rerunning_it(
    spec: runner_pb2.SessionSpec,
    start_runner: Callable[..., Awaitable[RunnerProcess]],
    driver: RunnerDriver,
    tmp_path: Path,
) -> None:
    pid_file, runs = tmp_path / "setup-pid", tmp_path / "setup-runs"
    script = (
        f"echo run >> {shlex.quote(str(runs))}\n"
        f"echo $$ > {shlex.quote(str(pid_file))}\n"
        'printf "started\\n"\n'
        "while :; do sleep 1; done\n"
    )
    first_runner = await start_runner()
    first = await driver.attach("setup-interrupted-by-kill", spec=spec, setup_script=script)
    await first.until(events.is_kind("setup_output"))
    setup_pid = int(pid_file.read_text())
    await first_runner.crash([setup_pid])

    await start_runner()
    second = await driver.attach("setup-interrupted-by-kill")
    assert second.attached.setup_state == runner_pb2.SETUP_STATE_INTERRUPTED
    await second.drain_until_end()
    assert len(events.of_kind(second.seen, "setup_started")) == 1
    assert len(events.of_kind(second.seen, "setup_interrupted")) == 1
    assert not events.of_kind(second.seen, "harness_started")
    assert runs.read_text() == "run\n"


async def test_runner_death_interrupts_bootstrap_without_rerunning_it(
    spec: runner_pb2.SessionSpec,
    start_runner: Callable[..., Awaitable[RunnerProcess]],
    driver: RunnerDriver,
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    # The script blocks writing its pid to this pipe until the test reads it.
    os.mkfifo(state / "started")
    script = "echo run >> runs\necho $$ > started\nwhile :; do sleep 1; done\n"
    first_runner = await start_runner(bootstrap=script)
    pid = int(await asyncio.to_thread((state / "started").read_text))
    await first_runner.crash([pid])

    await start_runner(bootstrap=script)
    first = await driver.attach("bootstrap-1", spec=spec)
    await first.submit_input("bootstrap-1-input", "Must not reach the harness.")
    await first.until(events.is_kind("harness_launch_failed"))
    second = await driver.attach("bootstrap-2", spec=spec)
    await second.submit_input("bootstrap-2-input", "Must not reach the harness.")
    await second.until(events.is_kind("command_failed"))

    assert len(events.of_kind(first.seen, "command_failed")) == 1
    assert not events.of_kind(first.seen, "harness_started")
    assert not events.of_kind(second.seen, "harness_started")
    assert (state / "runs").read_text() == "run\n"


@pytest.fixture
async def driver(gateway: Gateway, history_service: GoHistoryService) -> AsyncIterator[RunnerDriver]:
    """Drives whichever runner process holds the newest connection to `gateway`."""
    async with RunnerDriver(gateway, history_service.reader) as started:
        yield started


@pytest.fixture
def contend() -> Callable[[Path], Awaitable[None]]:
    """A competing nonblocking flock must fail while the runner owns its state directory."""

    async def attempt(state_dir: Path) -> None:
        descriptor = os.open(state_dir / ".agentplane-runner-owner", os.O_RDONLY)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            raise AssertionError("a contender acquired the runner state lock")
        finally:
            os.close(descriptor)

    return attempt


@pytest.mark.timeout(60)
async def test_competing_runner_cannot_take_state_and_successor_leaves_uncertain_dispatch_pending(
    contend: Callable[[Path], Awaitable[None]],
    model: ScriptedModel,
    spec: runner_pb2.SessionSpec,
    start_runner: Callable[..., Awaitable[RunnerProcess]],
    tmp_path: Path,
    driver: RunnerDriver,
) -> None:
    """A retained-state owner fences a contender; the successor does not replay an uncertain dispatch."""
    command_id = "fenced-replacement"
    first_runner = await start_runner(test_debug_checkpoint=("after-dispatch-planned", command_id))
    first = await driver.attach("writer-handoff-1", spec=spec)
    await first.submit_input("handoff-seed", "Reply with exactly: HANDOFF_SEED_OK")
    await model.reply(await model.request(), Text("HANDOFF_SEED_OK"))
    await first.until(events.turn_completed)
    await first.submit_input(command_id, "Reply with exactly: HANDOFF_REPLACEMENT_OK")
    await first.until(events.is_kind("command_admitted"))
    checkpoint = await first.until(events.is_kind("debug_checkpoint"))
    harness_pids = [entry.event.harness_started.pid for entry in events.of_kind(first.seen, "harness_started")]

    await contend(tmp_path / "state")

    await first_runner.crash(harness_pids)

    await start_runner()
    replacement = await driver.attach("writer-handoff-1", spec=spec, after_cursor=checkpoint.cursor)
    lost = await replacement.until(events.is_kind("harness_lost"))
    # The checkpoint pauses command dispatch, not the independent native-output reader.
    assert lost.cursor > checkpoint.cursor
    assert lost.origin.source_id == checkpoint.origin.source_id
    started = await replacement.until(events.is_kind("harness_started"))
    assert started.event.harness_started.resumed
    fresh_command_id = "fresh-after-fenced-dispatch"
    fresh_text = "Reply with exactly: HANDOFF_FRESH_OK"
    await replacement.submit_input(fresh_command_id, fresh_text)
    request = await model.request()
    assert request.user_texts[-1] == fresh_text
    assert "Reply with exactly: HANDOFF_REPLACEMENT_OK" not in request.user_texts
    assert model.request_count == 2
    await model.reply(request, Text("HANDOFF_FRESH_OK"))
    confirmed = await replacement.until(
        lambda entry: (
            events.kind(entry) == "harness_user_message_confirmed"
            and fresh_command_id in entry.event.harness_user_message_confirmed.origin_command_ids
        )
    )
    assert confirmed.event.harness_user_message_confirmed.origin_command_ids == [fresh_command_id]
    assert not any(
        command_id in entry.event.harness_user_message_confirmed.origin_command_ids
        for entry in events.of_kind(replacement.seen, "harness_user_message_confirmed")
    )
    await replacement.until(events.turn_completed)
    events.assert_contiguous([*first.seen, *replacement.seen])


async def test_runner_sigkill_fences_an_active_native_group_before_successor_dispatch(
    contend: Callable[[Path], Awaitable[None]],
    model: ScriptedModel,
    spec: runner_pb2.SessionSpec,
    start_runner: Callable[..., Awaitable[RunnerProcess]],
    tmp_path: Path,
    driver: RunnerDriver,
) -> None:
    """A successor cannot dispatch while the killed runner's native harness awaits upstream."""
    first_runner = await start_runner()
    first = await driver.attach("writer-handoff-active", spec=spec)
    await first.submit_input("active-before-crash", "Reply with exactly: ACTIVE_FENCE_OK")
    active_request = await model.request()
    assert active_request.user_texts[-1] == "Reply with exactly: ACTIVE_FENCE_OK"
    await model.hold(active_request)
    confirmed = await first.until(events.is_kind("harness_user_message_confirmed"))
    lost_turn_id = confirmed.event.harness_user_message_confirmed.turn_id
    harness_pids = [entry.event.harness_started.pid for entry in events.of_kind(first.seen, "harness_started")]

    await contend(tmp_path / "state")
    assert model.request_count == 1

    # ``crash`` waits the actual native harness PID, not merely the dead runner or supervisor.
    await first_runner.crash(harness_pids)

    await start_runner()
    await _wait_for_session_ready(driver, "writer-handoff-active")
    recovered = await driver.attach("writer-handoff-active", after_cursor=first.cursor)
    await recovered.until(events.is_kind("harness_lost"))
    started = await recovered.until(events.is_kind("harness_started"))
    assert started.event.harness_started.resumed
    reconciled = await recovered.until(
        lambda entry: (
            events.kind(entry) == "conversation_reconciled"
            and entry.event.conversation_reconciled.turn_id == lost_turn_id
        )
    )
    assert started.cursor < reconciled.cursor

    # Once the old group is fenced, a replacement owns the same state volume and can dispatch.
    replacement = await driver.attach("writer-handoff-successor", spec=spec)
    await replacement.submit_input("successor-input", "Reply with exactly: SUCCESSOR_FENCE_OK")
    successor_request = await model.request()
    assert successor_request.user_texts[-1] == "Reply with exactly: SUCCESSOR_FENCE_OK"
    assert model.request_count == 2
    await model.reply(successor_request, Text("SUCCESSOR_FENCE_OK"))
    await replacement.until(events.turn_completed)


async def test_a_restarted_runner_reports_the_loss_and_resumes_the_conversation(
    start_runner: Callable[[], Awaitable[RunnerProcess]],
    model: ScriptedModel,
    spec: runner_pb2.SessionSpec,
    driver: RunnerDriver,
) -> None:
    first_runner = await start_runner()
    first = await driver.attach("restart-1", spec=spec)
    await first.submit_input("input-1", "Reply with exactly: SEED_OK")
    request = await model.request()
    await model.reply(request, Text("SEED_OK"))
    await first.until(events.turn_completed)
    harness_pids = [entry.event.harness_started.pid for entry in events.of_kind(first.seen, "harness_started")]
    await first_runner.crash(harness_pids)

    await start_runner()
    second = await driver.attach("restart-1", spec=spec, after_cursor=first.cursor)
    lost = await second.until(events.is_kind("harness_lost"))
    assert second.attached.harness_state == runner_pb2.HARNESS_STATE_STOPPED
    await second.submit_input("input-2", "Reply with exactly: RESUMED_OK")
    started = await second.until(events.is_kind("harness_started"))
    assert lost.cursor < started.cursor
    assert started.event.harness_started.resumed
    assert not events.of_kind(second.seen, "turn_completed")
    request = await model.request()
    assert request.user_texts == ["Reply with exactly: SEED_OK", "Reply with exactly: RESUMED_OK"]
    assert request.assistant_texts == ["SEED_OK"]
    await model.reply(request, Text("RESUMED_OK"))
    done = await second.until(events.turn_completed)
    assert done.event.turn_completed.status == event_pb2.TURN_STATUS_COMPLETED
    events.assert_contiguous([*first.seen, *second.seen])


async def test_a_session_that_never_took_a_turn_carries_on_after_its_runner_restarts(
    start_runner: Callable[[], Awaitable[RunnerProcess]],
    model: ScriptedModel,
    spec: runner_pb2.SessionSpec,
    driver: RunnerDriver,
) -> None:
    """An unused reservation stays dormant across runner replacement and starts a fresh thread."""
    first_runner = await start_runner()
    first = await driver.attach("restart-before-first-turn", spec=spec)
    assert first.attached.harness_state == runner_pb2.HARNESS_STATE_STOPPED
    await first_runner.stop()

    await start_runner()
    second = await driver.attach("restart-before-first-turn", spec=spec, after_cursor=first.cursor)
    assert second.attached.harness_state == runner_pb2.HARNESS_STATE_STOPPED
    await second.submit_input("input-1", "Reply with exactly: FIRST_OK")
    started = await second.until(events.is_kind("harness_started"))
    assert not started.event.harness_started.resumed
    request = await model.request()
    assert request.user_texts == ["Reply with exactly: FIRST_OK"]
    await model.reply(request, Text("FIRST_OK"))
    done = await second.until(events.turn_completed)
    assert done.event.turn_completed.status == event_pb2.TURN_STATUS_COMPLETED


async def test_runner_startup_keeps_unselected_sessions_dormant_and_resume_keeps_history(
    start_runner: Callable[[], Awaitable[RunnerProcess]],
    model: ScriptedModel,
    spec: runner_pb2.SessionSpec,
    driver: RunnerDriver,
) -> None:
    """A restarted Sandbox keeps dormant native sessions stopped until one has pending work."""
    first_runner = await start_runner()

    async def seed_and_stop(session_id: str, suffix: str) -> tuple[int, str]:
        session = await driver.attach(session_id, spec=spec)
        await session.submit_input(f"{suffix}-seed", f"Reply with exactly: {suffix}_SEED_OK")
        await model.reply(await model.request(), Text(f"{suffix}_SEED_OK"))
        await session.until(events.turn_completed)
        (started,) = events.of_kind(session.seen, "harness_started")
        source_id = started.origin.source_id
        return session.cursor, source_id

    pending_cursor, pending_source_id = await seed_and_stop("pending-after-wake", "PENDING")
    dormant_cursor, dormant_source_id = await seed_and_stop("dormant-after-wake", "DORMANT")
    await first_runner.stop()

    await start_runner()
    summaries = {summary.session_id: summary for summary in await driver.list_sessions()}
    assert summaries[runner_test_session_id("pending-after-wake")].harness_state == runner_pb2.HARNESS_STATE_STOPPED
    assert summaries[runner_test_session_id("dormant-after-wake")].harness_state == runner_pb2.HARNESS_STATE_STOPPED

    pending = await driver.attach("pending-after-wake", after_cursor=pending_cursor)
    assert pending.attached.harness_state == runner_pb2.HARNESS_STATE_STOPPED
    await pending.submit_input("pending-after-wake-input", "Reply with exactly: PENDING_RESUMED_OK")
    pending_started = await pending.until(events.is_kind("harness_started"))
    assert pending_started.event.harness_started.resumed
    assert pending_started.origin.source_id == pending_source_id
    pending_request = await model.request()
    assert pending_request.user_texts == [
        "Reply with exactly: PENDING_SEED_OK",
        "Reply with exactly: PENDING_RESUMED_OK",
    ]
    assert pending_request.assistant_texts == ["PENDING_SEED_OK"]
    await model.reply(pending_request, Text("PENDING_RESUMED_OK"))
    await pending.until(events.turn_completed)
    summaries = {summary.session_id: summary for summary in await driver.list_sessions()}
    assert summaries[runner_test_session_id("dormant-after-wake")].harness_state == runner_pb2.HARNESS_STATE_STOPPED

    # A dormant session stays stopped until the next durable command wakes it.
    dormant = await driver.attach("dormant-after-wake", after_cursor=dormant_cursor)
    assert dormant.attached.harness_state == runner_pb2.HARNESS_STATE_STOPPED
    await dormant.submit_input("dormant-after-wake-input", "Reply with exactly: DORMANT_RESUMED_OK")
    dormant_started = await dormant.until(events.is_kind("harness_started"))
    assert dormant_started.event.harness_started.resumed
    assert dormant_started.origin.source_id == dormant_source_id
    dormant_request = await model.request()
    assert dormant_request.user_texts == [
        "Reply with exactly: DORMANT_SEED_OK",
        "Reply with exactly: DORMANT_RESUMED_OK",
    ]
    assert dormant_request.assistant_texts == ["DORMANT_SEED_OK"]
    await model.reply(dormant_request, Text("DORMANT_RESUMED_OK"))
    await dormant.until(events.turn_completed)


@pytest.mark.timeout(60)
async def test_crash_after_dispatch_planned_does_not_retry_uncertain_command(
    start_runner: Callable[..., Awaitable[RunnerProcess]],
    model: ScriptedModel,
    spec: runner_pb2.SessionSpec,
    driver: RunnerDriver,
) -> None:
    """This is intentionally a real runner/harness process test, not a mocked Session method.

    The checkpoint is after the committed ``dispatch_planned`` record and before either native
    adapter receives the command. After process death the runner cannot tell whether dispatch
    reached the harness, so it leaves that command unconfirmed and requires fresh input for progress.
    """
    command_id = "uncertain-after-dispatch-planned"
    uncertain_text = "Reply with exactly: UNCERTAIN_PAYLOAD_MUST_NOT_BE_RETRIED"
    first_runner = await start_runner(test_debug_checkpoint=("after-dispatch-planned", command_id))
    first = await driver.attach("restart-uncertain-dispatch", spec=spec)
    # Both native harnesses make a durable resume point only after one completed turn. This is the
    # normal target for a later command and keeps the crash test about command recovery, not a
    # harness's first-thread bootstrap contract.
    await first.submit_input("seed-before-uncertain-dispatch", "Reply with exactly: UNCERTAIN_SEED_OK")
    seed_request = await model.request()
    await model.reply(seed_request, Text("UNCERTAIN_SEED_OK"))
    await first.until(events.turn_completed)
    await first.submit_input(command_id, uncertain_text)
    await first.until(events.is_kind("command_admitted"))
    debug = await first.until(events.is_kind("debug_checkpoint"))
    assert (debug.event.debug_checkpoint.name, debug.event.debug_checkpoint.command_id) == (
        "after-dispatch-planned",
        command_id,
    )
    harness_pids = [entry.event.harness_started.pid for entry in events.of_kind(first.seen, "harness_started")]
    await first_runner.crash(harness_pids)

    # The first process is dead. The successor cannot safely reissue a command once its native
    # dispatch was planned, so that payload remains unconfirmed.
    await start_runner()
    second = await driver.attach("restart-uncertain-dispatch", spec=spec, after_cursor=first.cursor)
    started = await second.until(events.is_kind("harness_started"))
    assert started.event.harness_started.resumed
    fresh_command_id = "fresh-after-uncertain-dispatch"
    fresh_text = "Reply with exactly: FRESH_AFTER_UNCERTAIN_OK"
    await second.submit_input(fresh_command_id, fresh_text)
    request = await model.request()
    assert request.user_texts[-1] == fresh_text
    assert uncertain_text not in request.user_texts
    assert model.request_count == 2
    await model.reply(request, Text("FRESH_AFTER_UNCERTAIN_OK"))
    confirmed = await second.until(
        lambda entry: (
            events.kind(entry) == "harness_user_message_confirmed"
            and fresh_command_id in entry.event.harness_user_message_confirmed.origin_command_ids
        )
    )
    assert confirmed.event.harness_user_message_confirmed.origin_command_ids == [fresh_command_id]
    assert not any(
        command_id in entry.event.harness_user_message_confirmed.origin_command_ids
        for entry in events.of_kind(second.seen, "harness_user_message_confirmed")
    )
    await second.until(events.turn_completed)


@pytest.mark.timeout(60)
@pytest.mark.parametrize("harness", [runner_pb2.HARNESS_CODEX], ids=["codex"])
async def test_codex_unjournaled_turn_start_response_allows_fresh_input_without_resending(
    harness: runner_pb2.Harness,
    model: ScriptedModel,
    spec: runner_pb2.SessionSpec,
    start_runner: Callable[..., Awaitable[RunnerProcess]],
    driver: RunnerDriver,
) -> None:
    """A sent turn/start with an unjournaled response stays uncertain, but does not block new input."""
    command_id = "codex-turn-start-response-not-journaled"
    uncertain_text = "Reply with exactly: CODEX_UNJOURNALED_TURN_START_OK"
    first_runner = await start_runner(test_debug_checkpoint=("before-native-response-recorded", command_id))
    first = await driver.attach("codex-un-journaled-turn-start", spec=spec)

    # Resume requires an existing Codex thread with a completed turn.
    await first.submit_input("seed-before-un-journaled-turn-start", "Reply with exactly: SEED_OK")
    seed_request = await model.request()
    await model.reply(seed_request, Text("SEED_OK"))
    await first.until(events.turn_completed)

    await first.submit_input(command_id, uncertain_text)
    await first.until(events.is_kind("command_admitted"))
    checkpoint = await first.until(events.is_kind("debug_checkpoint"))
    assert (checkpoint.event.debug_checkpoint.name, checkpoint.event.debug_checkpoint.command_id) == (
        "before-native-response-recorded",
        command_id,
    )

    outgoing = _native_frames(first.seen, event_pb2.DIRECTION_TO_HARNESS)
    [turn_start] = [
        frame
        for frame in outgoing
        if frame.get("method") == "turn/start"
        and any(item.get("text") == uncertain_text for item in frame["params"]["input"])
    ]
    incoming = _native_frames(first.seen, event_pb2.DIRECTION_FROM_HARNESS)
    assert not any(frame.get("id") == turn_start["id"] for frame in incoming)
    assert not any(
        command_id in entry.event.harness_user_message_confirmed.origin_command_ids
        for entry in events.of_kind(first.seen, "harness_user_message_confirmed")
    )

    accepted = await model.request()
    assert accepted.user_texts.count(uncertain_text) == 1
    assert accepted.user_texts[-1] == uncertain_text
    await model.hold(accepted)
    harness_pids = [entry.event.harness_started.pid for entry in events.of_kind(first.seen, "harness_started")]
    await first_runner.crash(harness_pids)

    await start_runner()
    second = await driver.attach("codex-un-journaled-turn-start", spec=spec, after_cursor=first.cursor)
    started = await second.until(events.is_kind("harness_started"))
    assert started.event.harness_started.resumed

    fresh_command_id = "input-after-un-journaled-turn-start"
    fresh_text = "Reply with exactly: CODEX_FRESH_AFTER_UNJOURNALED_OK"
    await second.submit_input(fresh_command_id, fresh_text)
    async with asyncio.timeout(15):
        resumed = await model.request()
    assert resumed.user_texts.count(uncertain_text) == 1
    assert resumed.user_texts[-1] == fresh_text
    await model.reply(resumed, Text("CODEX_FRESH_AFTER_UNJOURNALED_OK"))

    confirmed = await second.until(
        lambda entry: (
            events.kind(entry) == "harness_user_message_confirmed"
            and fresh_command_id in entry.event.harness_user_message_confirmed.origin_command_ids
        )
    )
    assert confirmed.event.harness_user_message_confirmed.origin_command_ids == [fresh_command_id]
    assert not any(
        command_id in entry.event.harness_user_message_confirmed.origin_command_ids
        for entry in events.of_kind(second.seen, "harness_user_message_confirmed")
    )
    await second.until(events.turn_completed)

    all_seen = [*first.seen, *second.seen]
    turn_starts = [
        frame
        for frame in _native_frames(all_seen, event_pb2.DIRECTION_TO_HARNESS)
        if frame.get("method") == "turn/start"
    ]
    assert (
        len(
            [
                frame
                for frame in turn_starts
                if any(item.get("text") == uncertain_text for item in frame["params"]["input"])
            ]
        )
        == 1
    )
    assert (
        len(
            [frame for frame in turn_starts if any(item.get("text") == fresh_text for item in frame["params"]["input"])]
        )
        == 1
    )
    assert not any(
        entry.event.command_failed.command_id == command_id for entry in events.of_kind(all_seen, "command_failed")
    )
    assert not any(
        entry.event.command_noop.command_id == command_id for entry in events.of_kind(all_seen, "command_noop")
    )


@pytest.mark.timeout(60)
@pytest.mark.parametrize("harness", [runner_pb2.HARNESS_CODEX], ids=["codex"])
async def test_codex_acceptance_before_confirmation_crash_confirms_native_turn_without_resending_input(
    harness: runner_pb2.Harness,
    model: ScriptedModel,
    spec: runner_pb2.SessionSpec,
    start_runner: Callable[..., Awaitable[RunnerProcess]],
    driver: RunnerDriver,
) -> None:
    """Codex can persist a turn and tool result before the runner confirms its user message.

    On restart the runner reconciles that native turn and confirms the original command without
    resending the input. Fresh input then verifies the old user text and completed tool output each
    appear once in the model context.
    """
    command_id = "codex-accepted-before-confirmation"
    target_text = "Reply with exactly: CODEX_ACCEPTED_BEFORE_CONFIRMATION_OK"
    effect_file = Path(spec.cwd) / "side-effect.txt"
    first_runner = await start_runner(test_debug_checkpoint=("before-user-message-confirmed", command_id))
    first = await driver.attach("codex-accepted-before-confirmation", spec=spec)

    await first.submit_input("seed-before-acceptance-crash", "Reply with exactly: ACCEPTANCE_SEED_OK")
    seed_request = await model.request()
    await model.reply(seed_request, Text("ACCEPTANCE_SEED_OK"))
    await first.until(events.turn_completed)

    await first.submit_input(command_id, target_text)
    await first.until(events.is_kind("command_admitted"))
    checkpoint = await first.until(events.is_kind("debug_checkpoint"))
    assert (checkpoint.event.debug_checkpoint.name, checkpoint.event.debug_checkpoint.command_id) == (
        "before-user-message-confirmed",
        command_id,
    )
    assert not [
        entry
        for entry in events.of_kind(first.seen, "harness_user_message_confirmed")
        if command_id in entry.event.harness_user_message_confirmed.origin_command_ids
    ]
    original_turn_id = events.of_kind(first.seen, "turn_started")[-1].event.turn_started.turn_id

    accepted = await model.request()
    assert accepted.user_texts == ["Reply with exactly: ACCEPTANCE_SEED_OK", target_text]
    assert not accepted.tool_calls
    await model.reply(
        accepted,
        ShellCall("acceptance-side-effect", "printf 'ran\\n' >> side-effect.txt; printf 'SIDE_EFFECT_OUTPUT\\n'"),
    )
    tool_result_request = await model.request()
    assert any(
        result.call_id == "acceptance-side-effect" and result.contains("SIDE_EFFECT_OUTPUT")
        for result in tool_result_request.tool_outputs
    )
    await model.hold(tool_result_request)
    assert effect_file.read_text() == "ran\n"

    harness_pids = [entry.event.harness_started.pid for entry in events.of_kind(first.seen, "harness_started")]
    await first_runner.crash(harness_pids)

    await start_runner()
    second = await driver.attach("codex-accepted-before-confirmation", spec=spec, after_cursor=first.cursor)
    confirmed = await second.until(
        lambda entry: (
            events.kind(entry) == "harness_user_message_confirmed"
            and command_id in entry.event.harness_user_message_confirmed.origin_command_ids
        )
    )
    message = confirmed.event.harness_user_message_confirmed
    assert message.text == target_text
    assert message.origin_command_ids == [command_id]
    assert message.turn_id == original_turn_id
    started = next((entry for entry in events.of_kind(second.seen, "harness_started")), None)
    if started is None:
        started = await second.until(events.is_kind("harness_started"))
    assert started.event.harness_started.resumed

    fresh_command_id = "continue-after-native-acceptance"
    fresh_text = "Reply with exactly: AFTER_ACCEPTANCE_RESUME_OK"
    await second.submit_input(fresh_command_id, fresh_text)
    resumed_request = await model.request()
    assert resumed_request.user_texts.count(target_text) == 1
    assert resumed_request.user_texts[-1] == fresh_text
    (retained_output,) = [
        result for result in resumed_request.tool_outputs if result.call_id == "acceptance-side-effect"
    ]
    assert retained_output.contains("SIDE_EFFECT_OUTPUT")
    # Native resume retains the completed call/output without executing that old call again.
    assert effect_file.read_text() == "ran\n"
    await model.reply(resumed_request, Text("AFTER_ACCEPTANCE_RESUME_OK"))

    fresh_confirmed = await second.until(
        lambda entry: (
            events.kind(entry) == "harness_user_message_confirmed"
            and fresh_command_id in entry.event.harness_user_message_confirmed.origin_command_ids
        )
    )
    fresh_message = fresh_confirmed.event.harness_user_message_confirmed
    assert fresh_message.text == fresh_text
    assert fresh_message.origin_command_ids == [fresh_command_id]
    await second.until(events.turn_completed)
    assert effect_file.read_text() == "ran\n"


async def test_crash_after_terminal_effect_persists_replays_that_effect(
    start_runner: Callable[..., Awaitable[RunnerProcess]],
    model: ScriptedModel,
    spec: runner_pb2.SessionSpec,
    driver: RunnerDriver,
) -> None:
    """A committed terminal effect replays exactly after process death, without receipt repair."""
    command_id = "replay-persisted-effect"
    # This case needs only the real harness/runner protocol path; let dispatch proceed to the
    # native confirmation gate, then crash after its atomic command/Event commit.
    first_runner = await start_runner(test_debug_checkpoint=("after-terminal-outcome", command_id))
    first = await driver.attach("restart-effect-1", spec=spec)
    await first.submit_input("seed-before-effect", "Reply with exactly: EFFECT_SEED_OK")
    seed_request = await model.request()
    await model.reply(seed_request, Text("EFFECT_SEED_OK"))
    await first.until(events.turn_completed)
    await first.submit_input(command_id, "Reply with exactly: EFFECT_REPLAY_OK")
    await first.until(events.is_kind("command_admitted"))
    # Claude's confirmation correlation is its first response-side `message_start`, unlike
    # Codex's turn-start acknowledgement. Feed the real upstream response before waiting for the
    # harness-neutral terminal-effect checkpoint: either harness may then reach the same durable
    # runner boundary without the test assuming a shared native ordering.
    target_request = await model.request()
    await model.reply(target_request, Text("EFFECT_REPLAY_OK"))
    debug = await first.until(events.is_kind("debug_checkpoint"))
    assert (debug.event.debug_checkpoint.name, debug.event.debug_checkpoint.command_id) == (
        "after-terminal-outcome",
        command_id,
    )
    [published] = [
        entry
        for entry in events.of_kind(first.seen, "harness_user_message_confirmed")
        if command_id in entry.event.harness_user_message_confirmed.origin_command_ids
    ]
    # Command outcome and Event already committed together, before the checkpoint.
    assert published.cursor < debug.cursor
    lost_turn_id = published.event.harness_user_message_confirmed.turn_id
    harness_pids = [entry.event.harness_started.pid for entry in events.of_kind(first.seen, "harness_started")]
    await first_runner.crash(harness_pids)

    await start_runner()
    # Lost-turn recovery is runner-owned. Wait for READY before attaching so the follower snapshot
    # includes the replacement process and can consume its durable recovery events.
    await _wait_for_session_ready(driver, "restart-effect-1")
    second = await driver.attach("restart-effect-1")
    confirmed = await second.until(lambda entry: entry.cursor == published.cursor)
    assert confirmed.SerializeToString() == published.SerializeToString()
    assert confirmed.event.harness_user_message_confirmed.text == "Reply with exactly: EFFECT_REPLAY_OK"
    assert confirmed.event.harness_user_message_confirmed.origin_command_ids == [command_id]
    started = await second.until(events.is_kind("harness_started"))
    assert started.event.harness_started.resumed
    reconciled = await second.until(
        lambda entry: (
            events.kind(entry) == "conversation_reconciled"
            and entry.event.conversation_reconciled.turn_id == lost_turn_id
        )
    )
    assert started.cursor < reconciled.cursor
    assert (
        len(
            [
                entry
                for entry in events.of_kind(second.seen, "harness_user_message_confirmed")
                if command_id in entry.event.harness_user_message_confirmed.origin_command_ids
            ]
        )
        == 1
    )


async def test_sigterm_stops_the_harness_cleanly_and_a_new_incarnation_resumes(
    start_runner: Callable[..., Awaitable[RunnerProcess]],
    model: ScriptedModel,
    spec: runner_pb2.SessionSpec,
    driver: RunnerDriver,
    tmp_path: Path,
    workspace: Path,
) -> None:
    """A replaced Pod keeps only its state volume: the next runner starts with a new home and an
    empty workspace, and the native harness resumes from what the state directory holds."""
    first_runner = await start_runner(home=tmp_path / "first-home")
    first = await driver.attach("sigterm-1", spec=spec)
    await first.submit_input("input-1", "Reply with exactly: SEED_OK")
    seed = await model.request()
    await model.reply(seed, Text("SEED_OK"))
    await first.until(events.turn_completed)
    (running,) = await driver.list_sessions()
    assert running.session_id == first.attached.session_id
    assert running.harness_state == runner_pb2.HARNESS_STATE_RUNNING
    assert running.spec == first.attached.spec
    # Native frames after the turn's result may still be arriving.
    assert running.last_cursor >= first.cursor
    # The runner exits only after the stop ladder has reaped every harness.
    await first_runner.stop()
    shutil.rmtree(tmp_path / "first-home")
    shutil.rmtree(workspace)

    await start_runner(home=tmp_path / "second-home")
    (stopped,) = await driver.list_sessions()
    assert stopped.harness_state == runner_pb2.HARNESS_STATE_STOPPED
    assert stopped.last_cursor > first.cursor
    second = await driver.attach("sigterm-1", spec=spec, after_cursor=first.cursor)
    exited = await second.until(events.is_kind("harness_exited"))
    assert exited.event.harness_exited.stopped_by_runner
    await second.submit_input("input-2", "Reply with exactly: RESUMED_OK")
    started = await second.until(events.is_kind("harness_started"))
    assert started.event.harness_started.resumed
    assert not events.of_kind(second.seen, "harness_lost")
    request = await model.request()
    assert request.user_texts == ["Reply with exactly: SEED_OK", "Reply with exactly: RESUMED_OK"]
    assert request.assistant_texts == ["SEED_OK"]
    # An unchanged system prompt keeps the provider's prompt cache usable across the incarnation.
    assert request.system_text == seed.system_text
    await model.reply(request, Text("RESUMED_OK"))
    done = await second.until(events.turn_completed)
    assert done.event.turn_completed.status == event_pb2.TURN_STATUS_COMPLETED
    events.assert_contiguous([*first.seen, *second.seen])


async def test_a_reasoning_item_cut_off_by_a_harness_kill_is_reported_absent_and_not_replayed(
    start_runner: Callable[[], Awaitable[RunnerProcess]],
    model: ScriptedModel,
    spec: runner_pb2.SessionSpec,
    driver: RunnerDriver,
) -> None:
    first_runner = await start_runner()
    first = await driver.attach("cut-off-reasoning-1", spec=spec)
    await first.submit_input("input-1", "Reply with exactly: SEED_OK")
    await model.hold_after_first_delta(await model.request(), Reasoning("CUT_OFF_THOUGHT"))
    cut_off = (await first.until(events.is_kind("item_started"))).event.item_started.item_id
    await first.until(events.is_kind("text_delta"))
    harness_pids = [entry.event.harness_started.pid for entry in events.of_kind(first.seen, "harness_started")]
    await first_runner.crash(harness_pids)

    await start_runner()
    second = await driver.attach("cut-off-reasoning-1", spec=spec, after_cursor=first.cursor)
    await second.until(events.is_kind("harness_started"))
    # The reconciliation the resumed harness makes is the last word on the lost turn's items.
    reconciled = await second.until(events.is_kind("conversation_reconciled"))
    assert [(item.item_id, item.disposition) for item in reconciled.event.conversation_reconciled.items] == [
        (cut_off, event_pb2.RECOVERY_DISPOSITION_ABSENT)
    ]
    await second.submit_input("input-2", "Reply with exactly: RESUMED_OK")
    request = await model.request()
    assert request.reasoning_texts == []
    await model.reply(request, Text("RESUMED_OK"))
    await second.until(events.turn_completed)


@pytest.mark.timeout(60)
@pytest.mark.parametrize("harness", [runner_pb2.HARNESS_CODEX], ids=["codex"])
async def test_codex_failed_turn_recovery_uses_selected_paginated_history(
    driver: RunnerDriver,
    endpoint: object,
    model: ScriptedModel,
    spec: runner_pb2.SessionSpec,
    start_runner: Callable[..., Awaitable[RunnerProcess]],
) -> None:
    """The pinned binary recovers selected history pages after its runner process is replaced."""
    assert isinstance(endpoint, OpenAIResponses)
    endpoint.serve_catalog([model_catalog.bundled("gpt-6-luna", slug=spec.model)])

    session_id = "codex-paginated-recovery"
    first_runner = await start_runner()
    first = await driver.attach(session_id, spec=spec)

    # An older completed turn makes an all-history scan visible in the public JSON-RPC archive.
    await first.submit_input("old-turn", "Reply with exactly: OLD_TURN_ONLY")
    old_request = await model.request()
    await model.reply(old_request, Text("OLD_TURN_ONLY"))
    old_turn = await first.until(events.turn_completed)

    await first.submit_input("seed-turn", "Reply with exactly: LATEST_SEED_OK")
    seed_request = await model.request()
    await model.reply(seed_request, Text("LATEST_SEED_OK"))
    seed_turn = await first.until(events.turn_completed)

    before_recovery_turn = first.cursor
    await first.submit_input("interrupted-turn", "Think through this, run the tool, then wait.")

    request = await model.request()
    assert request.native.code_mode
    reasoning_summaries = [f"recovery reasoning {index:03}" for index in range(101)]
    answer: list[Reasoning | Text] = [Reasoning(summary) for summary in reasoning_summaries]
    answer.append(Text("RECOVERY_ASSISTANT_TEXT"))
    await model.reply(request, *answer, ShellCall("call_history_array", "printf CODEX_HISTORY_OUTPUT"))

    tool_request = await model.request()
    custom_output = next(
        output for output in tool_request.native.custom_tool_call_outputs if output.call_id == "call_history_array"
    )
    assert isinstance(custom_output.output, list)
    assert any("CODEX_HISTORY_OUTPUT" in part.text for part in custom_output.output)
    assert tool_request.assistant_texts[-1] == "RECOVERY_ASSISTANT_TEXT"
    pre_crash_reasoning = [
        (item.encrypted_content, [part.text for part in item.summary]) for item in tool_request.native.reasoning
    ]
    assert [summary for _, summaries in pre_crash_reasoning for summary in summaries] == reasoning_summaries

    tool_started = await first.until(
        lambda entry: (
            entry.cursor > before_recovery_turn
            and events.kind(entry) == "item_started"
            and entry.event.item_started.kind == event_pb2.ITEM_KIND_TOOL_CALL
            and entry.event.item_started.tool_name == "commandExecution"
        )
    )
    tool_call_id = tool_started.event.item_started.item_id
    await first.until(
        lambda entry: events.kind(entry) == "item_completed" and entry.event.item_completed.item_id == tool_call_id
    )
    active_turn = next(
        entry.event.turn_started.turn_id
        for entry in first.seen
        if entry.cursor > before_recovery_turn and events.kind(entry) == "turn_started"
    )
    reasoning_ids = [
        entry.event.item_started.item_id
        for entry in first.seen
        if entry.cursor > before_recovery_turn
        and events.kind(entry) == "item_started"
        and entry.event.item_started.kind == event_pb2.ITEM_KIND_REASONING
    ]
    assistant_ids = [
        entry.event.item_started.item_id
        for entry in first.seen
        if entry.cursor > before_recovery_turn
        and events.kind(entry) == "item_started"
        and entry.event.item_started.kind == event_pb2.ITEM_KIND_ASSISTANT_TEXT
    ]
    assert len(reasoning_ids) == 101
    assert len(assistant_ids) == 1
    failed_request = tool_request
    for attempt in range(3):
        if attempt:
            failed_request = await model.request()
        assert any(
            output.call_id == "call_history_array" and output.contains("CODEX_HISTORY_OUTPUT")
            for output in failed_request.tool_outputs
        )
        await model.http_error(failed_request)
    failed = await first.until(events.turn_completed)
    assert failed.event.turn_completed.status == event_pb2.TURN_STATUS_FAILED

    harness_pids = [entry.event.harness_started.pid for entry in events.of_kind(first.seen, "harness_started")]
    await first_runner.crash(harness_pids)

    await start_runner()
    reconnected = await driver.attach(session_id, after_cursor=first.cursor)
    await reconnected.submit_input("continue-after-restart", "Continue and reply CONTINUED_OK.")
    started = await reconnected.until(events.is_kind("harness_started"))
    assert started.event.harness_started.resumed
    reconciled = await reconnected.until(
        lambda entry: (
            events.kind(entry) == "conversation_reconciled"
            and entry.event.conversation_reconciled.turn_id == active_turn
        )
    )
    report = {item.item_id: item for item in reconciled.event.conversation_reconciled.items}
    assert set(reasoning_ids + assistant_ids + [tool_call_id]) <= report.keys()
    assert all(report[item_id].disposition == event_pb2.RECOVERY_DISPOSITION_RETAINED for item_id in reasoning_ids)
    assert report[assistant_ids[0]].disposition == event_pb2.RECOVERY_DISPOSITION_RETAINED
    assert report[tool_call_id].disposition == event_pb2.RECOVERY_DISPOSITION_RETAINED

    # API reads include the newest completed turn and the interrupted turn, but not older history.
    pages_by_turn = _codex_history_items(
        reconnected.seen, recovery_turn_ids={seed_turn.event.turn_completed.turn_id, active_turn}, require_resume=True
    )
    assert set(pages_by_turn) == {seed_turn.event.turn_completed.turn_id, active_turn}
    assert active_turn in pages_by_turn
    assert old_turn.event.turn_completed.turn_id not in pages_by_turn
    active_items = pages_by_turn[active_turn]
    assert len(active_items) > 100
    assert len([item for item in active_items if item.get("type") == "reasoning"]) == 101
    assert {summary for item in active_items if item.get("type") == "reasoning" for summary in item["summary"]} == set(
        reasoning_summaries
    )
    assert [item["text"] for item in active_items if item.get("type") == "agentMessage"] == ["RECOVERY_ASSISTANT_TEXT"]
    assert any(
        item.get("type") == "commandExecution" and "CODEX_HISTORY_OUTPUT" in item.get("aggregatedOutput", "")
        for item in active_items
    )

    request = await model.request()
    assert request.user_texts[-1] == "Continue and reply CONTINUED_OK."
    assert [
        (item.encrypted_content, [part.text for part in item.summary]) for item in request.native.reasoning
    ] == pre_crash_reasoning
    assert "RECOVERY_ASSISTANT_TEXT" in request.assistant_texts
    assert any(
        output.call_id == "call_history_array" and output.contains("CODEX_HISTORY_OUTPUT")
        for output in request.tool_outputs
    )
    await model.reply(request, Text("CONTINUED_OK"))
    continued = await reconnected.until(events.turn_completed)
    assert continued.event.turn_completed.status == event_pb2.TURN_STATUS_COMPLETED
    events.assert_contiguous([*first.seen, *reconnected.seen])


@pytest.mark.timeout(60)
@pytest.mark.parametrize("harness", [runner_pb2.HARNESS_CODEX], ids=["codex"])
async def test_codex_normal_interrupt_reconciles_on_the_live_command_loop(
    driver: RunnerDriver,
    endpoint: object,
    model: ScriptedModel,
    spec: runner_pb2.SessionSpec,
    start_runner: Callable[..., Awaitable[RunnerProcess]],
) -> None:
    assert isinstance(endpoint, OpenAIResponses)
    endpoint.serve_catalog([model_catalog.bundled("gpt-6-luna", slug=spec.model)])

    first_runner = await start_runner()
    first = await driver.attach("codex-normal-interrupt", spec=spec)
    before_interrupted_turn = first.cursor
    await first.submit_input("interrupt-input", "Run the tool, then wait for my interrupt.")
    request = await model.request()
    await model.reply(
        request,
        Reasoning("INTERRUPTED_REASONING"),
        Text("INTERRUPTED_ASSISTANT_TEXT"),
        ShellCall("call_interrupted_tool", "printf INTERRUPTED_TOOL_OUTPUT"),
    )
    tool_request = await model.request()
    assert any(
        output.call_id == "call_interrupted_tool" and output.contains("INTERRUPTED_TOOL_OUTPUT")
        for output in tool_request.tool_outputs
    )
    tool_started = await first.until(
        lambda entry: (
            events.kind(entry) == "item_started"
            and entry.event.item_started.kind == event_pb2.ITEM_KIND_TOOL_CALL
            and entry.event.item_started.tool_name == "commandExecution"
        )
    )
    tool_call_id = tool_started.event.item_started.item_id
    await first.until(
        lambda entry: events.kind(entry) == "item_completed" and entry.event.item_completed.item_id == tool_call_id
    )
    active_turn = next(
        entry.event.turn_started.turn_id
        for entry in first.seen
        if entry.cursor > before_interrupted_turn and events.kind(entry) == "turn_started"
    )
    await model.hold(tool_request)

    await first.interrupt_turn("interrupt-now", active_turn)
    interrupted = await first.until(
        lambda entry: events.kind(entry) == "turn_completed" and entry.event.turn_completed.turn_id == active_turn
    )
    assert interrupted.event.turn_completed.status == event_pb2.TURN_STATUS_INTERRUPTED
    reconciled = await first.until(
        lambda entry: (
            events.kind(entry) == "conversation_reconciled"
            and entry.event.conversation_reconciled.turn_id == active_turn
        )
    )
    assert reconciled.event.conversation_reconciled.items
    interrupted_report = {item.item_id: item for item in reconciled.event.conversation_reconciled.items}
    assert interrupted_report[tool_call_id].disposition == event_pb2.RECOVERY_DISPOSITION_RETAINED
    pages_by_turn = _codex_history_items(first.seen, recovery_turn_ids={active_turn})
    assert set(pages_by_turn) == {active_turn}
    assert any(
        item.get("type") == "agentMessage" and item.get("text") == "INTERRUPTED_ASSISTANT_TEXT"
        for item in pages_by_turn[active_turn]
    )
    assert any(
        item.get("type") == "commandExecution" and "INTERRUPTED_TOOL_OUTPUT" in item.get("aggregatedOutput", "")
        for item in pages_by_turn[active_turn]
    )
    await first_runner.stop()


if __name__ == "__main__":
    pytest_bazel.main()
