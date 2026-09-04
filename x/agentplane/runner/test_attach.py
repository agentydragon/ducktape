"""Attachments come and go; the session and its log do not."""

from __future__ import annotations

import pytest
import pytest_bazel

from x.agentplane.runner import protocol_pb2 as pb
from x.agentplane.runner.client import RunnerClient, RunnerError
from x.agentplane.runner.testing import events
from x.agentplane.runner.testing.scripted_model import ScriptedModel, ShellCall, Text

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf

WAIT_COMMAND = 'sh -c \'printf "wait_started\\n"; sleep 3; printf "wait_finished\\n"\''


async def test_reattach_resumes_from_the_cursor_without_gap_or_duplicate(
    client: RunnerClient, model: ScriptedModel, spec: pb.SessionSpec
) -> None:
    first = await client.attach("reattach-1", spec=spec)
    await first.send("input-1", "Wait with the shell, then reply.")
    request = await model.request()
    model.reply(request, ShellCall("call_test_1", WAIT_COMMAND))
    await first.until(events.is_kind("tool_arguments"))
    # The connection drops mid-turn; the harness keeps running the tool.
    first.cancel()

    second = await client.attach("reattach-1", after_sequence=first.cursor)
    assert second.attached.harness == pb.HARNESS_STATE_RUNNING
    assert second.attached.active_turn_id == events.of_kind(first.seen, "turn_started")[-1].turn_started.turn_id
    request = await model.request()
    assert "wait_finished" in request.tool_outputs[0].text
    model.reply(request, Text("RECONNECTED_OK"))
    done = await second.until(events.turn_completed)
    assert done.turn_completed.status == pb.TURN_STATUS_COMPLETED

    combined = [*first.seen, *second.seen]
    events.assert_contiguous(combined)
    assert len(events.of_kind(combined, "harness_started")) == 1
    assert len(events.of_kind(combined, "turn_started")) == 1
    await second.detach()
    await second.drain_until_end()
    model.assert_quiescent()


async def test_replay_from_zero_returns_the_whole_log(
    client: RunnerClient, model: ScriptedModel, spec: pb.SessionSpec
) -> None:
    first = await client.attach("replay-1", spec=spec)
    await first.send("input-1", "Reply with exactly: REPLAY_SEED_OK")
    request = await model.request()
    model.reply(request, Text("REPLAY_SEED_OK"))
    await first.until(events.turn_completed)
    await first.detach()
    await first.drain_until_end()

    second = await client.attach("replay-1")
    assert second.attached.last_sequence == first.seen[-1].sequence
    replayed = [await second.next_event() for _ in first.seen]
    assert [event.SerializeToString() for event in replayed] == [event.SerializeToString() for event in first.seen]
    await second.detach()
    await second.drain_until_end()
    model.assert_quiescent()


async def test_resending_an_input_id_delivers_it_once(
    client: RunnerClient, model: ScriptedModel, spec: pb.SessionSpec
) -> None:
    first = await client.attach("retry-1", spec=spec)
    await first.send("input-1", "Reply with exactly: ONCE_OK")
    request = await model.request()
    await first.until(events.is_kind("input_accepted"))
    first.cancel()

    second = await client.attach("retry-1", after_sequence=first.cursor)
    await second.send("input-1", "Reply with exactly: ONCE_OK")
    again = await second.until(events.is_kind("input_accepted"))
    assert again.input_accepted.input_id == "input-1"
    assert again.input_accepted.turn_id == events.of_kind(first.seen, "input_accepted")[-1].input_accepted.turn_id
    assert request.user_texts.count("Reply with exactly: ONCE_OK") == 1
    model.reply(request, Text("ONCE_OK"))
    await second.until(events.turn_completed)
    assert len(events.of_kind([*first.seen, *second.seen], "input_submitted")) == 1
    await second.detach()
    await second.drain_until_end()
    model.assert_quiescent()


async def test_a_newer_attachment_supersedes_the_current_one(
    client: RunnerClient, model: ScriptedModel, spec: pb.SessionSpec
) -> None:
    first = await client.attach("supersede-1", spec=spec)
    second = await client.attach("supersede-1", after_sequence=first.cursor)
    with pytest.raises(RunnerError, match="superseded"):
        await first.until(lambda _: False, timeout_s=30)
    await second.send("input-1", "Reply with exactly: SECOND_OK")
    request = await model.request()
    model.reply(request, Text("SECOND_OK"))
    await second.until(events.turn_completed)
    await second.detach()
    await second.drain_until_end()
    model.assert_quiescent()


async def test_shutdown_stops_the_harness_and_open_resumes_the_conversation(
    client: RunnerClient, model: ScriptedModel, spec: pb.SessionSpec
) -> None:
    first = await client.attach("shutdown-1", spec=spec)
    await first.send("input-1", "Reply with exactly: SEED_OK")
    request = await model.request()
    model.reply(request, Text("SEED_OK"))
    await first.until(events.turn_completed)
    await first.shutdown()
    exited = await first.until(events.is_kind("harness_exited"))
    assert exited.harness_exited.stopped_by_runner
    await first.drain_until_end()

    second = await client.attach("shutdown-1", after_sequence=first.cursor)
    started = await second.until(events.is_kind("harness_started"))
    assert started.harness_started.resumed
    assert second.attached.harness == pb.HARNESS_STATE_RUNNING
    await second.send("input-2", "Reply with exactly: RESUMED_OK")
    request = await model.request()
    assert request.user_texts == ["Reply with exactly: SEED_OK", "Reply with exactly: RESUMED_OK"]
    assert request.assistant_texts == ["SEED_OK"]
    model.reply(request, Text("RESUMED_OK"))
    done = await second.until(events.turn_completed)
    assert done.turn_completed.status == pb.TURN_STATUS_COMPLETED
    await second.detach()
    await second.drain_until_end()
    model.assert_quiescent()


async def test_open_rejects_a_mismatched_spec(client: RunnerClient, spec: pb.SessionSpec) -> None:
    first = await client.attach("spec-1", spec=spec)
    await first.detach()
    await first.drain_until_end()
    other = pb.SessionSpec(
        provider=spec.provider,
        cwd=spec.cwd,
        model="agentplane-test/other-model",
        reasoning_effort=spec.reasoning_effort,
    )
    with pytest.raises(RunnerError, match="different spec"):
        await client.attach("spec-1", spec=other)
    with pytest.raises(RunnerError, match="does not exist"):
        await client.attach("spec-2")
    instructed = pb.SessionSpec(
        provider=spec.provider,
        cwd=spec.cwd,
        model=spec.model,
        reasoning_effort=spec.reasoning_effort,
        instructions="Standing order the session was not created with.",
    )
    with pytest.raises(RunnerError, match="different spec"):
        await client.attach("spec-1", spec=instructed)
    relative = pb.SessionSpec(provider=spec.provider, cwd="work/../elsewhere", model=spec.model)
    with pytest.raises(RunnerError, match="absolute"):
        await client.attach("spec-3", spec=relative)


if __name__ == "__main__":
    pytest_bazel.main()
