"""Codex Sessions of one Runner share one app-server; each keeps its own turns, tools and recovery."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import pytest_bazel

from agentplane.proto import event_pb2, runner_pb2
from agentplane.runner.testing import events
from agentplane.runner.testing.driver import RunnerDriver, SessionFollow, SessionSealedError
from agentplane.runner.testing.process import kill_harness
from agentplane.testing.scripted_model import ModelRequest, ScriptedModel, ShellCall, Text

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf

pytestmark = pytest.mark.parametrize("harness", [runner_pb2.HARNESS_CODEX], ids=["codex"])

SESSION_IDS = ("shared-first", "shared-second")


async def _open(driver: RunnerDriver, spec: runner_pb2.SessionSpec, workspace: Path) -> dict[str, SessionFollow]:
    sessions = {}
    for session_id in SESSION_IDS:
        session_spec = runner_pb2.SessionSpec()
        session_spec.CopyFrom(spec)
        session_spec.cwd = str(workspace / session_id)
        sessions[session_id] = await driver.attach(session_id, spec=session_spec)
    return sessions


async def _submit_to_each(sessions: dict[str, SessionFollow], command_id: str, text: str) -> None:
    """Admit one input in every Session at once, so their harnesses start concurrently."""
    await asyncio.gather(
        *(session.submit_input(command_id, f"{text} ({session_id})") for session_id, session in sessions.items())
    )


async def _requests_by_session(model: ScriptedModel, count: int) -> dict[str, ModelRequest]:
    """The next model request of each Session, told apart by the Session ID its input names."""
    requests = {}
    for _ in range(count):
        request = await model.request()
        (session_id,) = [session_id for session_id in SESSION_IDS if session_id in "\n".join(request.user_texts)]
        requests[session_id] = request
    return requests


async def _codex_process(driver: RunnerDriver) -> runner_pb2.HarnessProcessStatus:
    runner = await driver.gateway.control.runner()
    status: runner_pb2.RunnerStatus = await runner.GetStatus(runner_pb2.GetStatusRequest())
    (process,) = status.processes
    assert process.harness == runner_pb2.HARNESS_CODEX
    return process


async def test_codex_sessions_share_one_app_server_and_keep_their_own_turns_and_tools(
    driver: RunnerDriver, model: ScriptedModel, spec: runner_pb2.SessionSpec, workspace: Path
) -> None:
    sessions = await _open(driver, spec, workspace)
    await _submit_to_each(sessions, "input-1", "Run the tool, then reply TOOL_DONE")

    requests = await _requests_by_session(model, len(sessions))
    for session_id, request in requests.items():
        await model.reply(request, ShellCall(f"call-{session_id}", 'printf "%s|%s" "$AGENTPLANE_DESTINATION" "$PWD"'))
    followups = await _requests_by_session(model, len(sessions))
    for session_id, request in followups.items():
        (output,) = request.tool_outputs
        (other,) = set(SESSION_IDS) - {session_id}
        # Each thread's tools see their own Session's destination and working directory.
        assert output.contains(f'"session_id":"{session_id}"}}|{workspace / session_id}')
        assert not output.contains(f'"session_id":"{other}"')
        await model.reply(request, Text(f"TOOL_DONE {session_id}"))

    started_pids = set()
    for session_id, session in sessions.items():
        done = await session.until(events.turn_completed)
        assert done.event.turn_completed.status == event_pb2.TURN_STATUS_COMPLETED
        (text,) = events.items(session.seen, event_pb2.ITEM_KIND_ASSISTANT_TEXT)
        assert events.completed(session.seen, text).text == f"TOOL_DONE {session_id}"
        (started,) = events.of_kind(session.seen, "harness_started")
        started_pids.add(started.event.harness_started.pid)
        events.assert_contiguous(session.seen)
        events.assert_sourced(session.seen)
    (pid,) = started_pids
    process = await _codex_process(driver)
    assert process.pid == pid
    assert set(process.assigned_session_ids) == set(SESSION_IDS)


async def test_interrupting_one_codex_session_leaves_the_other_running(
    driver: RunnerDriver, model: ScriptedModel, spec: runner_pb2.SessionSpec, workspace: Path
) -> None:
    sessions = await _open(driver, spec, workspace)
    await _submit_to_each(sessions, "input-1", "Wait; do not answer early")
    requests = await _requests_by_session(model, len(sessions))
    for request in requests.values():
        await model.hold(request)
    first, second = (sessions[session_id] for session_id in SESSION_IDS)
    confirmed = await first.until(events.is_kind("harness_user_message_confirmed"))
    await second.until(events.is_kind("harness_user_message_confirmed"))

    await first.interrupt_turn("interrupt-1", confirmed.event.harness_user_message_confirmed.turn_id)
    interrupted = await first.until(events.turn_completed)
    assert interrupted.event.turn_completed.status == event_pb2.TURN_STATUS_INTERRUPTED
    assert interrupted.event.turn_completed.interrupted_by_command_id == "interrupt-1"

    held = requests[SESSION_IDS[1]]
    await model.reply(held, Text("SECOND_FINISHED"))
    finished = await second.until(events.turn_completed)
    assert finished.event.turn_completed.status == event_pb2.TURN_STATUS_COMPLETED
    assert not events.of_kind(second.seen, "harness_exited")


async def test_a_shared_app_server_crash_loses_every_hosted_turn_and_each_session_resumes(
    driver: RunnerDriver, model: ScriptedModel, spec: runner_pb2.SessionSpec, workspace: Path
) -> None:
    sessions = await _open(driver, spec, workspace)
    await _submit_to_each(sessions, "input-1", "Wait; do not answer early")
    for request in (await _requests_by_session(model, len(sessions))).values():
        await model.hold(request)
    for session in sessions.values():
        await session.until(events.is_kind("harness_user_message_confirmed"))
    crashed = await _codex_process(driver)

    runner = await driver.gateway.control.runner()
    await kill_harness(await runner.GetStatus(runner_pb2.GetStatusRequest()), SESSION_IDS[0])
    for session in sessions.values():
        lost = await session.until(events.turn_completed)
        assert lost.event.turn_completed.status == event_pb2.TURN_STATUS_PROCESS_LOST
        exited = await session.until(events.is_kind("harness_exited"))
        assert exited.cursor > lost.cursor
        assert not exited.event.harness_exited.stopped_by_runner

    await _submit_to_each(sessions, "input-2", "Reply with exactly: RESUMED_OK")
    for session_id, request in (await _requests_by_session(model, len(sessions))).items():
        await model.reply(request, Text(f"RESUMED_OK {session_id}"))
    for session in sessions.values():
        done = await session.until(events.turn_completed)
        assert done.event.turn_completed.status == event_pb2.TURN_STATUS_COMPLETED
        started = events.of_kind(session.seen, "harness_started")
        assert len(started) == 2
        assert started[-1].event.harness_started.resumed
    resumed = await _codex_process(driver)
    assert resumed.process_id != crashed.process_id
    assert set(resumed.assigned_session_ids) == set(SESSION_IDS)


async def test_quiesce_interrupts_every_codex_session_before_the_shared_app_server_stops(
    driver: RunnerDriver, model: ScriptedModel, spec: runner_pb2.SessionSpec, workspace: Path
) -> None:
    sessions = await _open(driver, spec, workspace)
    await _submit_to_each(sessions, "input-1", "Wait; do not answer early")
    for request in (await _requests_by_session(model, len(sessions))).values():
        await model.hold(request)
    for session in sessions.values():
        await session.until(events.is_kind("harness_user_message_confirmed"))

    await driver.quiesce()
    for session in sessions.values():
        with pytest.raises(SessionSealedError):
            await session.until(lambda _: False)
        (done,) = events.of_kind(session.seen, "turn_completed")
        assert done.event.turn_completed.status == event_pb2.TURN_STATUS_INTERRUPTED
        assert done.event.turn_completed.ended_by_teardown
        exited = events.of_kind(session.seen, "harness_exited")[-1]
        assert exited.event.harness_exited.stopped_by_runner
        assert exited.cursor > done.cursor


if __name__ == "__main__":
    pytest_bazel.main()
