"""Followers come and go; the session and its log do not."""

from __future__ import annotations

import asyncio
from pathlib import Path

import grpc
import pytest
import pytest_bazel

from agentplane.proto import event_pb2, runner_pb2
from agentplane.runner.testing import events
from agentplane.runner.testing.commands import submit_input
from agentplane.runner.testing.driver import RunnerDriver, SessionSealedError, refusal
from agentplane.runner.testing.scripted_model import Reasoning, ScriptedModel, ShellCall, Text

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf

WAIT_COMMAND = 'sh -c \'printf "wait_started\\n"; sleep 3; printf "wait_finished\\n"\''


async def test_a_new_follow_resumes_from_the_cursor_without_gap_or_duplicate(
    driver: RunnerDriver, model: ScriptedModel, spec: runner_pb2.SessionSpec
) -> None:
    first = await driver.attach("reattach-1", spec=spec)
    await submit_input(first, "input-1", "Wait with the shell, then reply.")
    request = await model.request()
    await model.reply(request, ShellCall("call_test_1", WAIT_COMMAND))
    await first.until(events.is_kind("tool_arguments"))
    # The first follow is abandoned mid-turn; the harness keeps running the tool.

    second = await driver.attach("reattach-1", after_cursor=first.cursor)
    assert second.attached.harness_state == runner_pb2.HARNESS_STATE_RUNNING
    assert second.attached.active_turn_id == events.of_kind(first.seen, "turn_started")[-1].event.turn_started.turn_id
    request = await model.request()
    assert "wait_finished" in request.tool_outputs[0].text
    await model.reply(request, Text("RECONNECTED_OK"))
    done = await second.until(events.turn_completed)
    assert done.event.turn_completed.status == event_pb2.TURN_STATUS_COMPLETED

    combined = [*first.seen, *second.seen]
    events.assert_contiguous(combined)
    assert len(events.of_kind(combined, "harness_started")) == 1
    assert len(events.of_kind(combined, "turn_started")) == 1


async def test_replay_from_zero_returns_the_whole_log(
    driver: RunnerDriver, model: ScriptedModel, spec: runner_pb2.SessionSpec
) -> None:
    first = await driver.attach("replay-1", spec=spec)
    await submit_input(first, "input-1", "Reply with exactly: REPLAY_SEED_OK")
    request = await model.request()
    await model.reply(request, Text("REPLAY_SEED_OK"))
    await first.until(events.turn_completed)

    second = await driver.attach("replay-1")
    # Native frames after the turn's result may still have arrived.
    assert second.attached.last_cursor >= first.seen[-1].cursor
    replayed = [await second.next_entry() for _ in first.seen]
    assert [entry.SerializeToString() for entry in replayed] == [entry.SerializeToString() for entry in first.seen]


async def test_resending_a_command_id_delivers_it_once(
    driver: RunnerDriver, model: ScriptedModel, spec: runner_pb2.SessionSpec
) -> None:
    first = await driver.attach("retry-1", spec=spec)
    await submit_input(first, "input-1", "Reply with exactly: ONCE_OK")
    request = await model.request()
    # Claude's exact confirmation evidence is its response-side model-message start; Codex obtains
    # it at native turn start. Both have sent one upstream request before this reply, and both reach
    # the one common harness-confirmation effect after it.
    await model.reply(request, Text("ONCE_OK"))
    confirmed = await first.until(events.is_kind("harness_user_message_confirmed"))

    second = await driver.attach("retry-1", after_cursor=first.cursor)
    await submit_input(second, "input-1", "Reply with exactly: ONCE_OK")
    assert confirmed.event.harness_user_message_confirmed.origin_command_ids == ["input-1"]
    assert request.user_texts.count("Reply with exactly: ONCE_OK") == 1
    await second.until(events.turn_completed)
    assert len(events.of_kind([*first.seen, *second.seen], "command_admitted")) == 1


async def test_followers_share_events_and_a_command_delivered_twice_at_once_runs_once(
    driver: RunnerDriver, model: ScriptedModel, spec: runner_pb2.SessionSpec
) -> None:
    first = await driver.attach("observers-1", spec=spec)
    await first.until(events.is_kind("harness_started"))
    replay_cursor = first.cursor
    second = await driver.attach("observers-1", after_cursor=replay_cursor)
    await asyncio.gather(
        submit_input(first, "input-1", "Reply with exactly: SECOND_OK"),
        submit_input(second, "input-1", "Reply with exactly: SECOND_OK"),
    )
    request = await model.request()
    await model.reply(request, Text("SECOND_OK"))
    await second.until(events.turn_completed)
    await first.until(events.turn_completed)
    assert [entry for entry in first.seen if entry.cursor > replay_cursor] == second.seen
    events.assert_contiguous(first.seen)
    assert len(events.of_kind(first.seen, "command_admitted")) == 1
    assert request.user_texts.count("Reply with exactly: SECOND_OK") == 1
    await submit_input(first, "input-2", "Reply with exactly: FIRST_STILL_HERE")
    await model.reply(await model.request(), Text("FIRST_STILL_HERE"))
    await first.until(events.turn_completed)


async def test_shutdown_stops_the_harness_and_open_resumes_the_conversation(
    driver: RunnerDriver, model: ScriptedModel, spec: runner_pb2.SessionSpec
) -> None:
    first = await driver.attach("shutdown-1", spec=spec)
    await submit_input(first, "input-1", "Reply with exactly: SEED_OK")
    request = await model.request()
    await model.reply(request, Text("SEED_OK"))
    await first.until(events.turn_completed)
    observer = await driver.attach("shutdown-1", after_cursor=first.cursor)
    await first.stop_runner_session("stop-1")
    exited = await first.until(events.is_kind("harness_exited"))
    assert exited.event.harness_exited.stopped_by_runner
    await first.drain_until_end()
    await observer.drain_until_end()
    assert events.of_kind(observer.seen, "harness_exited")[-1] == exited

    replay = await driver.attach("shutdown-1")
    assert replay.attached.harness_state == runner_pb2.HARNESS_STATE_STOPPED
    await replay.drain_until_end()
    assert replay.seen == first.seen
    (summary,) = await driver.list_sessions()
    assert summary.harness_state == runner_pb2.HARNESS_STATE_STOPPED

    second = await driver.attach("shutdown-1", spec=spec, after_cursor=first.cursor)
    started = await second.until(events.is_kind("harness_started"))
    assert started.event.harness_started.resumed
    assert second.attached.harness_state == runner_pb2.HARNESS_STATE_RUNNING
    await submit_input(second, "input-2", "Reply with exactly: RESUMED_OK")
    request = await model.request()
    assert request.user_texts == ["Reply with exactly: SEED_OK", "Reply with exactly: RESUMED_OK"]
    assert request.assistant_texts == ["SEED_OK"]
    await model.reply(request, Text("RESUMED_OK"))
    done = await second.until(events.turn_completed)
    assert done.event.turn_completed.status == event_pb2.TURN_STATUS_COMPLETED


async def test_seal_stops_the_running_harness_and_ends_live_followers(
    driver: RunnerDriver, model: ScriptedModel, spec: runner_pb2.SessionSpec
) -> None:
    first = await driver.attach("seal-1", spec=spec)
    await submit_input(first, "input-1", "Reply with exactly: SEED_OK")
    await model.reply(await model.request(), Text("SEED_OK"))
    await first.until(events.turn_completed)
    response = await driver.quiesce()
    assert response.HasField("stage")
    assert not response.sessions
    seal = driver.gateway.ingress.sealed["seal-1"]
    with pytest.raises(SessionSealedError) as sealed:
        await first.until(lambda _: False)
    assert sealed.value.cursor == seal == first.cursor
    assert events.of_kind(first.seen, "harness_exited")[-1].event.harness_exited.stopped_by_runner
    assert await refusal(driver.attach("seal-1", spec=spec)) == grpc.StatusCode.ABORTED


async def test_resume_sends_the_model_the_reasoning_of_the_previous_harness(
    driver: RunnerDriver, model: ScriptedModel, spec: runner_pb2.SessionSpec
) -> None:
    first = await driver.attach("reasoning-1", spec=spec)
    await submit_input(first, "input-1", "Reply with exactly: SEED_OK")
    await model.reply(await model.request(), Reasoning("SEED_THOUGHT"), Text("SEED_OK"))
    await first.until(events.turn_completed)
    await first.stop_runner_session("stop-1")
    await first.until(events.is_kind("harness_exited"))
    await first.drain_until_end()

    second = await driver.attach("reasoning-1", spec=spec, after_cursor=first.cursor)
    started = await second.until(events.is_kind("harness_started"))
    assert started.event.harness_started.resumed
    await submit_input(second, "input-2", "Reply with exactly: RESUMED_OK")
    request = await model.request()
    assert request.reasoning_texts == ["SEED_THOUGHT"]
    await model.reply(request, Text("RESUMED_OK"))
    await second.until(events.turn_completed)


async def test_open_rejects_a_mismatched_spec(driver: RunnerDriver, spec: runner_pb2.SessionSpec) -> None:
    await driver.attach("spec-1", spec=spec)
    other = runner_pb2.SessionSpec(
        harness=spec.harness, cwd=spec.cwd, model="agentplane-test/other-model", reasoning_effort=spec.reasoning_effort
    )
    assert await refusal(driver.attach("spec-1", spec=other)) == grpc.StatusCode.ALREADY_EXISTS
    assert await refusal(driver.attach("spec-2")) == grpc.StatusCode.NOT_FOUND
    instructed = runner_pb2.SessionSpec(
        harness=spec.harness,
        cwd=spec.cwd,
        model=spec.model,
        reasoning_effort=spec.reasoning_effort,
        instructions="Standing order the session was not created with.",
    )
    assert await refusal(driver.attach("spec-1", spec=instructed)) == grpc.StatusCode.ALREADY_EXISTS
    relative = runner_pb2.SessionSpec(harness=spec.harness, cwd="work/../elsewhere", model=spec.model)
    assert await refusal(driver.attach("spec-3", spec=relative)) == grpc.StatusCode.INVALID_ARGUMENT


async def test_setup_runs_in_thread_cwd_before_harness_and_replays_output(
    driver: RunnerDriver, spec: runner_pb2.SessionSpec
) -> None:
    gate = Path(spec.cwd) / "release-setup"
    script = 'pwd; printf "setup stderr\\n" >&2; while [ ! -f release-setup ]; do sleep 0.05; done'
    first = await driver.attach("setup-success", spec=spec, setup_script=script)
    assert first.attached.setup_state == runner_pb2.SETUP_STATE_RUNNING
    assert first.attached.harness_state == runner_pb2.HARNESS_STATE_STOPPED
    await first.until(events.is_kind("setup_output"))
    assert not events.of_kind(first.seen, "harness_started")
    gate.write_text("go")
    finished = await first.until(events.is_kind("setup_finished"))
    assert finished.event.setup_finished.exit_code == 0
    await first.until(events.is_kind("harness_started"))
    assert [entry.event.WhichOneof("observation") for entry in first.seen].index("setup_finished") < [
        entry.event.WhichOneof("observation") for entry in first.seen
    ].index("harness_started")
    stdout = b"".join(
        entry.event.setup_output.stdout
        for entry in events.of_kind(first.seen, "setup_output")
        if entry.event.setup_output.WhichOneof("stream") == "stdout"
    )
    assert stdout == f"{spec.cwd}\n".encode()
    replay = await driver.attach("setup-success")
    assert replay.attached.setup_state == runner_pb2.SETUP_STATE_SUCCEEDED
    copied = [await replay.next_entry() for _ in first.seen]
    assert [entry.SerializeToString() for entry in copied] == [entry.SerializeToString() for entry in first.seen]


async def test_failed_setup_is_terminal_and_reopen_never_runs_it_again(
    driver: RunnerDriver, spec: runner_pb2.SessionSpec
) -> None:
    script = 'printf "one attempt\\n"; exit 7'
    first = await driver.attach("setup-failure", spec=spec, setup_script=script)
    finished = await first.until(events.is_kind("setup_finished"))
    assert finished.event.setup_finished.exit_code == 7
    await first.drain_until_end()
    assert not events.of_kind(first.seen, "harness_started")
    second = await driver.attach("setup-failure", spec=spec)
    assert second.attached.setup_state == runner_pb2.SETUP_STATE_FAILED
    assert second.attached.harness_state == runner_pb2.HARNESS_STATE_STOPPED
    await second.drain_until_end()
    assert len(events.of_kind(second.seen, "setup_started")) == 1
    assert len(events.of_kind(second.seen, "setup_finished")) == 1
    assert (
        await refusal(driver.attach("setup-failure", spec=spec, setup_script="exit 0"))
        == grpc.StatusCode.ALREADY_EXISTS
    )


async def test_setup_exit_before_consuming_stdin_keeps_its_exit_status(
    driver: RunnerDriver, spec: runner_pb2.SessionSpec
) -> None:
    script = "exit 3\n" + ("# filler\n" * 7_000)
    first = await driver.attach("setup-early-exit", spec=spec, setup_script=script)
    finished = await first.until(events.is_kind("setup_finished"))
    assert finished.event.setup_finished.exit_code == 3
    await first.drain_until_end()
    assert not events.of_kind(first.seen, "setup_interrupted")


if __name__ == "__main__":
    pytest_bazel.main()
