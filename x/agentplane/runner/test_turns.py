"""Turns through the runner: one script per scenario, run against both harnesses."""

from __future__ import annotations

import json

import pytest_bazel

from x.agentplane.runner import protocol_pb2 as pb
from x.agentplane.runner.client import RunnerClient
from x.agentplane.runner.testing import events
from x.agentplane.runner.testing.scripted_model import Reasoning, ScriptedModel, ShellCall, Text

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf

WAIT_COMMAND = 'sh -c \'printf "wait_started\\n"; sleep 3; printf "wait_finished\\n"\''


async def test_one_turn_streams_reasoning_and_text(
    client: RunnerClient, model: ScriptedModel, spec: pb.SessionSpec
) -> None:
    session = await client.attach("turn-1", spec=spec)
    assert session.attached.harness == pb.HARNESS_STATE_RUNNING
    assert session.attached.active_turn_id == ""
    await session.send("input-1", "Reply with exactly: BASELINE_OK")

    request = await model.request()
    assert request.streaming
    assert request.user_texts[-1] == "Reply with exactly: BASELINE_OK"
    model.reply(request, Reasoning("brief"), Text("BASELINE_OK"))

    done = await session.until(events.turn_completed)
    assert done.turn_completed.status == pb.TURN_STATUS_COMPLETED
    seen = session.seen
    (started,) = events.of_kind(seen, "harness_started")
    assert not started.harness_started.resumed
    (turn,) = events.of_kind(seen, "turn_started")
    assert turn.turn_started.turn_id == done.turn_completed.turn_id
    (submitted,) = events.of_kind(seen, "input_submitted")
    (accepted,) = events.of_kind(seen, "input_accepted")
    assert submitted.input_submitted.input_id == accepted.input_accepted.input_id == "input-1"
    assert accepted.input_accepted.turn_id == turn.turn_started.turn_id
    assert turn.sequence < accepted.sequence < done.sequence

    (reasoning,) = events.items(seen, pb.ITEM_KIND_REASONING)
    assert events.streamed_text(seen, reasoning) == "brief"
    assert events.completed(seen, reasoning).text == "brief"
    (text,) = events.items(seen, pb.ITEM_KIND_ASSISTANT_TEXT)
    assert events.streamed_text(seen, text) == "BASELINE_OK"
    assert events.completed(seen, text).text == "BASELINE_OK"
    assert not events.items(seen, pb.ITEM_KIND_TOOL_CALL)
    events.assert_contiguous(seen)
    events.assert_sourced(seen)
    assert {event.native.direction for event in events.of_kind(seen, "native")} == {
        pb.DIRECTION_TO_HARNESS,
        pb.DIRECTION_FROM_HARNESS,
    }
    await session.detach()
    await session.drain_until_end()
    model.assert_quiescent()


async def test_tool_call_reports_arguments_and_result(
    client: RunnerClient, model: ScriptedModel, spec: pb.SessionSpec
) -> None:
    session = await client.attach("tool-1", spec=spec)
    await session.send("input-1", "Run the tool and report TOOL_DONE.")

    request = await model.request()
    model.reply(request, ShellCall("call_test_1", "printf TOOL_OUTPUT"))
    request = await model.request()
    (output,) = request.tool_outputs
    assert output.call_id == "call_test_1"
    assert "TOOL_OUTPUT" in output.text
    model.reply(request, Text("TOOL_DONE"))

    done = await session.until(events.turn_completed)
    assert done.turn_completed.status == pb.TURN_STATUS_COMPLETED
    seen = session.seen
    (call,) = events.items(seen, pb.ITEM_KIND_TOOL_CALL)
    (started,) = [event for event in events.of_kind(seen, "item_started") if event.item_started.item_id == call]
    assert started.item_started.tool_name
    assert "printf TOOL_OUTPUT" in json.dumps(json.loads(events.tool_arguments(seen, call)))
    result = events.completed(seen, call)
    assert result.HasField("tool")
    assert result.tool.succeeded
    (text,) = events.items(seen, pb.ITEM_KIND_ASSISTANT_TEXT)
    assert events.completed(seen, text).text == "TOOL_DONE"
    assert [events.kind(event) for event in seen if events.kind(event) in ("item_started", "turn_completed")] == [
        "item_started",
        "item_started",
        "turn_completed",
    ]
    events.assert_sourced(seen)
    await session.detach()
    await session.drain_until_end()
    model.assert_quiescent()


async def test_failed_tool_is_reported_as_failed(
    client: RunnerClient, model: ScriptedModel, spec: pb.SessionSpec
) -> None:
    session = await client.attach("tool-2", spec=spec)
    await session.send("input-1", "Run the failing tool.")

    request = await model.request()
    model.reply(request, ShellCall("call_test_1", "sh -c 'printf failing; exit 23'"))
    request = await model.request()
    (output,) = request.tool_outputs
    assert "failing" in output.text
    model.reply(request, Text("SEEN_FAILURE"))

    await session.until(events.turn_completed)
    (call,) = events.items(session.seen, pb.ITEM_KIND_TOOL_CALL)
    assert not events.completed(session.seen, call).tool.succeeded
    await session.detach()
    await session.drain_until_end()
    model.assert_quiescent()


async def test_input_during_a_turn_joins_it(client: RunnerClient, model: ScriptedModel, spec: pb.SessionSpec) -> None:
    session = await client.attach("join-1", spec=spec)
    await session.send("input-1", "Wait with the shell, then reply.")

    request = await model.request()
    model.reply(request, ShellCall("call_test_1", WAIT_COMMAND))
    await session.until(events.is_kind("tool_arguments"))
    await session.send("input-2", "Reply ONLY SECOND_INPUT_OBSERVED after your current work.")
    accepted = await session.until(
        lambda event: events.kind(event) == "input_accepted" and event.input_accepted.input_id == "input-2"
    )
    assert (
        accepted.input_accepted.turn_id == session.attached.active_turn_id
        or accepted.input_accepted.turn_id == events.of_kind(session.seen, "turn_started")[-1].turn_started.turn_id
    )

    request = await model.request()
    assert "wait_finished" in request.tool_outputs[0].text
    assert any(
        "SECOND_INPUT_OBSERVED" in text
        for text in request.user_texts + [output.text for output in request.tool_outputs]
    )
    model.reply(request, Text("SECOND_INPUT_OBSERVED"))

    done = await session.until(events.turn_completed)
    assert done.turn_completed.status == pb.TURN_STATUS_COMPLETED
    assert len(events.of_kind(session.seen, "turn_started")) == 1
    assert [event.input_accepted.input_id for event in events.of_kind(session.seen, "input_accepted")] == [
        "input-1",
        "input-2",
    ]
    await session.detach()
    await session.drain_until_end()
    model.assert_quiescent()


async def test_interrupt_ends_the_turn_as_interrupted(
    client: RunnerClient, model: ScriptedModel, spec: pb.SessionSpec
) -> None:
    session = await client.attach("interrupt-1", spec=spec)
    await session.send("input-1", "Wait; do not answer early.")

    request = await model.request()
    model.hold(request)
    await session.until(events.is_kind("input_accepted"))
    await session.interrupt()

    done = await session.until(events.turn_completed)
    assert done.turn_completed.status == pb.TURN_STATUS_INTERRUPTED
    assert not events.of_kind(session.seen, "item_completed")

    await session.send("input-2", "Reply with exactly: AFTER_INTERRUPT_OK")
    request = await model.request()
    assert request.user_texts[-1] == "Reply with exactly: AFTER_INTERRUPT_OK"
    model.reply(request, Text("AFTER_INTERRUPT_OK"))
    done = await session.until(events.turn_completed)
    assert done.turn_completed.status == pb.TURN_STATUS_COMPLETED
    await session.detach()
    await session.drain_until_end()
    model.assert_quiescent()


if __name__ == "__main__":
    pytest_bazel.main()
