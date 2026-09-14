"""Attachments come and go; the session and its log do not."""

from __future__ import annotations

import asyncio

import pytest
import pytest_bazel

from x.agentplane.protocol import event_pb2
from x.agentplane.runner import protocol_pb2
from x.agentplane.runner.client import RunnerClient, RunnerError
from x.agentplane.runner.testing import events
from x.agentplane.runner.testing.scripted_model import ScriptedModel, ShellCall, Text

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf

WAIT_COMMAND = 'sh -c \'printf "wait_started\\n"; sleep 3; printf "wait_finished\\n"\''


async def test_reattach_resumes_from_the_cursor_without_gap_or_duplicate(
    client: RunnerClient, model: ScriptedModel, spec: protocol_pb2.SessionSpec
) -> None:
    first = await client.attach("reattach-1", spec=spec)
    await first.send("input-1", "Wait with the shell, then reply.")
    request = await model.request()
    await model.reply(request, ShellCall("call_test_1", WAIT_COMMAND))
    await first.until(events.is_kind("tool_arguments"))
    # The connection drops mid-turn; the harness keeps running the tool.
    first.cancel()

    second = await client.attach("reattach-1", after_cursor=first.cursor)
    assert second.attached.harness_state == protocol_pb2.HARNESS_STATE_RUNNING
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
    await second.detach()
    await second.drain_until_end()


async def test_replay_from_zero_returns_the_whole_log(
    client: RunnerClient, model: ScriptedModel, spec: protocol_pb2.SessionSpec
) -> None:
    first = await client.attach("replay-1", spec=spec)
    await first.send("input-1", "Reply with exactly: REPLAY_SEED_OK")
    request = await model.request()
    await model.reply(request, Text("REPLAY_SEED_OK"))
    await first.until(events.turn_completed)
    await first.detach()
    await first.drain_until_end()

    second = await client.attach("replay-1")
    assert second.attached.last_cursor == first.seen[-1].cursor
    replayed = [await second.next_entry() for _ in first.seen]
    assert [entry.SerializeToString() for entry in replayed] == [entry.SerializeToString() for entry in first.seen]
    await second.detach()
    await second.drain_until_end()


async def test_resending_a_command_id_delivers_it_once(
    client: RunnerClient, model: ScriptedModel, spec: protocol_pb2.SessionSpec
) -> None:
    first = await client.attach("retry-1", spec=spec)
    await first.send("input-1", "Reply with exactly: ONCE_OK")
    request = await model.request()
    # Claude's exact confirmation evidence is its response-side model-message start; Codex obtains
    # it at native turn start. Both have sent one upstream request before this reply, and both reach
    # the one common harness-confirmation effect after it.
    await model.reply(request, Text("ONCE_OK"))
    confirmed = await first.until(events.is_kind("harness_user_message_confirmed"))
    first.cancel()

    second = await client.attach("retry-1", after_cursor=first.cursor)
    await second.send("input-1", "Reply with exactly: ONCE_OK")
    assert confirmed.event.harness_user_message_confirmed.origin_command_ids == ["input-1"]
    assert request.user_texts.count("Reply with exactly: ONCE_OK") == 1
    await second.until(events.turn_completed)
    assert len(events.of_kind([*first.seen, *second.seen], "command_admitted")) == 1
    await second.detach()
    await second.drain_until_end()


async def test_attachments_share_events_and_detach_independently(
    client: RunnerClient, model: ScriptedModel, spec: protocol_pb2.SessionSpec
) -> None:
    first = await client.attach("observers-1", spec=spec)
    await first.until(events.is_kind("harness_started"))
    replay_cursor = first.cursor
    second = await client.attach("observers-1", after_cursor=replay_cursor)
    await asyncio.gather(
        first.send("input-1", "Reply with exactly: SECOND_OK"), second.send("input-1", "Reply with exactly: SECOND_OK")
    )
    request = await model.request()
    await model.reply(request, Text("SECOND_OK"))
    await second.until(events.turn_completed)
    await first.until(events.turn_completed)
    assert [entry for entry in first.seen if entry.cursor > replay_cursor] == second.seen
    events.assert_contiguous(first.seen)
    assert len(events.of_kind(first.seen, "command_admitted")) == 1
    assert request.user_texts.count("Reply with exactly: SECOND_OK") == 1
    await second.detach()
    await second.drain_until_end()
    await first.send("input-2", "Reply with exactly: FIRST_STILL_HERE")
    await model.reply(await model.request(), Text("FIRST_STILL_HERE"))
    await first.until(events.turn_completed)
    await first.detach()
    await first.drain_until_end()


async def test_shutdown_stops_the_harness_and_open_resumes_the_conversation(
    client: RunnerClient, model: ScriptedModel, spec: protocol_pb2.SessionSpec
) -> None:
    first = await client.attach("shutdown-1", spec=spec)
    await first.send("input-1", "Reply with exactly: SEED_OK")
    request = await model.request()
    await model.reply(request, Text("SEED_OK"))
    await first.until(events.turn_completed)
    observer = await client.attach("shutdown-1", after_cursor=first.cursor)
    await first.stop_runner_session("stop-1")
    exited = await first.until(events.is_kind("harness_exited"))
    assert exited.event.harness_exited.stopped_by_runner
    await first.drain_until_end()
    await observer.drain_until_end()
    assert events.of_kind(observer.seen, "harness_exited")[-1] == exited

    replay = await client.attach("shutdown-1")
    assert replay.attached.harness_state == protocol_pb2.HARNESS_STATE_STOPPED
    await replay.drain_until_end()
    assert replay.seen == first.seen
    (summary,) = await client.list_sessions()
    assert summary.harness_state == protocol_pb2.HARNESS_STATE_STOPPED

    second = await client.attach("shutdown-1", spec=spec, after_cursor=first.cursor)
    started = await second.until(events.is_kind("harness_started"))
    assert started.event.harness_started.resumed
    assert second.attached.harness_state == protocol_pb2.HARNESS_STATE_RUNNING
    await second.send("input-2", "Reply with exactly: RESUMED_OK")
    request = await model.request()
    assert request.user_texts == ["Reply with exactly: SEED_OK", "Reply with exactly: RESUMED_OK"]
    assert request.assistant_texts == ["SEED_OK"]
    await model.reply(request, Text("RESUMED_OK"))
    done = await second.until(events.turn_completed)
    assert done.event.turn_completed.status == event_pb2.TURN_STATUS_COMPLETED
    await second.detach()
    await second.drain_until_end()


async def test_open_rejects_a_mismatched_spec(client: RunnerClient, spec: protocol_pb2.SessionSpec) -> None:
    first = await client.attach("spec-1", spec=spec)
    await first.detach()
    await first.drain_until_end()
    other = protocol_pb2.SessionSpec(
        harness=spec.harness, cwd=spec.cwd, model="agentplane-test/other-model", reasoning_effort=spec.reasoning_effort
    )
    with pytest.raises(RunnerError, match="different spec"):
        await client.attach("spec-1", spec=other)
    with pytest.raises(RunnerError, match="does not exist"):
        await client.attach("spec-2")
    instructed = protocol_pb2.SessionSpec(
        harness=spec.harness,
        cwd=spec.cwd,
        model=spec.model,
        reasoning_effort=spec.reasoning_effort,
        instructions="Standing order the session was not created with.",
    )
    with pytest.raises(RunnerError, match="different spec"):
        await client.attach("spec-1", spec=instructed)
    relative = protocol_pb2.SessionSpec(harness=spec.harness, cwd="work/../elsewhere", model=spec.model)
    with pytest.raises(RunnerError, match="absolute"):
        await client.attach("spec-3", spec=relative)


if __name__ == "__main__":
    pytest_bazel.main()
