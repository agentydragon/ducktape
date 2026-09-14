"""Turns through the runner: one script per scenario, run against both harnesses."""

from __future__ import annotations

import json

import pytest
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
    assert session.attached.harness_state == pb.HARNESS_STATE_RUNNING
    assert session.attached.active_turn_id == ""
    await session.send("input-1", "Reply with exactly: BASELINE_OK")

    request = await model.request()
    assert request.streaming
    assert request.user_texts[-1] == "Reply with exactly: BASELINE_OK"
    await model.reply(request, Reasoning("brief"), Text("BASELINE_OK"))

    done = await session.until(events.turn_completed)
    assert done.turn_completed.status == pb.TURN_STATUS_COMPLETED
    seen = session.seen
    (started,) = events.of_kind(seen, "harness_started")
    assert not started.harness_started.resumed
    (turn,) = events.of_kind(seen, "turn_started")
    assert turn.turn_started.turn_id == done.turn_completed.turn_id
    (received,) = events.of_kind(seen, "command_received")
    (confirmed,) = events.of_kind(seen, "harness_user_message_confirmed")
    assert received.command_received.command_id == "input-1"
    assert confirmed.harness_user_message_confirmed.origin_command_ids == ["input-1"]
    assert confirmed.harness_user_message_confirmed.text == "Reply with exactly: BASELINE_OK"
    assert confirmed.harness_user_message_confirmed.turn_id == turn.turn_started.turn_id
    assert received.sequence < confirmed.sequence < done.sequence

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


async def test_tool_call_reports_arguments_and_result(
    client: RunnerClient, model: ScriptedModel, spec: pb.SessionSpec
) -> None:
    session = await client.attach("tool-1", spec=spec)
    await session.send("input-1", "Run the tool and report TOOL_DONE.")

    request = await model.request()
    await model.reply(request, ShellCall("call_test_1", "printf TOOL_OUTPUT"))
    request = await model.request()
    (output,) = request.tool_outputs
    assert output.call_id == "call_test_1"
    assert "TOOL_OUTPUT" in output.text
    await model.reply(request, Text("TOOL_DONE"))

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


async def test_failed_tool_is_reported_as_failed(
    client: RunnerClient, model: ScriptedModel, spec: pb.SessionSpec
) -> None:
    session = await client.attach("tool-2", spec=spec)
    await session.send("input-1", "Run the failing tool.")

    request = await model.request()
    await model.reply(request, ShellCall("call_test_1", "sh -c 'printf failing; exit 23'"))
    request = await model.request()
    (output,) = request.tool_outputs
    assert "failing" in output.text
    await model.reply(request, Text("SEEN_FAILURE"))

    await session.until(events.turn_completed)
    (call,) = events.items(session.seen, pb.ITEM_KIND_TOOL_CALL)
    assert not events.completed(session.seen, call).tool.succeeded
    await session.detach()
    await session.drain_until_end()


@pytest.mark.parametrize("harness", [pb.HARNESS_CODEX])
async def test_codex_input_during_a_turn_joins_it(
    client: RunnerClient, model: ScriptedModel, spec: pb.SessionSpec
) -> None:
    session = await client.attach("join-1", spec=spec)
    await session.send("input-1", "Wait with the shell, then reply.")

    request = await model.request()
    await model.reply(request, ShellCall("call_test_1", WAIT_COMMAND))
    await session.until(events.is_kind("tool_arguments"))
    await session.send("input-2", "Reply ONLY SECOND_INPUT_OBSERVED after your current work.")
    confirmed = await session.until(
        lambda event: (
            events.kind(event) == "harness_user_message_confirmed"
            and event.harness_user_message_confirmed.origin_command_ids == ["input-2"]
        )
    )
    assert (
        confirmed.harness_user_message_confirmed.turn_id == session.attached.active_turn_id
        or confirmed.harness_user_message_confirmed.turn_id
        == events.of_kind(session.seen, "turn_started")[-1].turn_started.turn_id
    )

    request = await model.request()
    assert "wait_finished" in request.tool_outputs[0].text
    assert any(
        "SECOND_INPUT_OBSERVED" in text
        for text in request.user_texts + [output.text for output in request.tool_outputs]
    )
    await model.reply(request, Text("SECOND_INPUT_OBSERVED"))

    done = await session.until(events.turn_completed)
    assert done.turn_completed.status == pb.TURN_STATUS_COMPLETED
    assert len(events.of_kind(session.seen, "turn_started")) == 1
    assert [
        event.harness_user_message_confirmed.origin_command_ids
        for event in events.of_kind(session.seen, "harness_user_message_confirmed")
    ] == [["input-1"], ["input-2"]]
    await session.detach()
    await session.drain_until_end()


@pytest.mark.parametrize("harness", [pb.HARNESS_CLAUDE])
async def test_claude_inputs_during_a_tool_are_confirmed_from_the_started_cohort(
    client: RunnerClient, model: ScriptedModel, spec: pb.SessionSpec
) -> None:
    """Claude folds active-turn inputs into its tool-result continuation, without a user echo."""
    session = await client.attach("claude-tool-input-1", spec=spec)
    await session.send("input-1", "Wait with the shell, then reply.")

    request = await model.request()
    await model.reply(request, ShellCall("call_test_1", WAIT_COMMAND))
    await session.until(events.is_kind("tool_arguments"))
    await session.send("input-2", "Reply ONLY SECOND_INPUT_OBSERVED after your current work.")
    await session.send("input-3", "Reply ONLY THIRD_INPUT_OBSERVED after your current work.")

    request = await model.request()
    assert "wait_finished" in request.tool_outputs[0].text
    assert "SECOND_INPUT_OBSERVED" in request.tool_outputs[0].text
    assert "THIRD_INPUT_OBSERVED" in request.tool_outputs[0].text
    await model.reply(request, Text("SECOND_INPUT_OBSERVED"))

    done = await session.until(events.turn_completed)
    assert done.turn_completed.status == pb.TURN_STATUS_COMPLETED
    confirmations = events.of_kind(session.seen, "harness_user_message_confirmed")
    assert [event.harness_user_message_confirmed.origin_command_ids for event in confirmations] == [
        ["input-1"],
        ["input-2", "input-3"],
    ]
    assert confirmations[-1].harness_user_message_confirmed.text == (
        "Reply ONLY SECOND_INPUT_OBSERVED after your current work.\n"
        "Reply ONLY THIRD_INPUT_OBSERVED after your current work."
    )
    assert len(confirmations[-1].source_sequences) == 3  # tool result, then Claude's two lifecycle starts.
    await session.detach()
    await session.drain_until_end()


async def test_model_command_reaches_the_first_upstream_request_that_selects_it(
    client: RunnerClient, model: ScriptedModel, harness: pb.Harness, spec: pb.SessionSpec
) -> None:
    """Drive the pinned harnesses through the loopback server, rather than trusting runner state."""
    attached = await client.attach("switch-model-1", spec=spec)
    selected = (
        "agentplane-switched/claude-haiku-4-5-20251001" if harness == pb.HARNESS_CLAUDE else "agentplane-switched-model"
    )
    await attached.switch_model("switch-1", selected)
    received = await attached.until(events.is_kind("command_received"))
    assert received.command_received.command_id == "switch-1"

    await attached.send("input-1", "Reply with exactly: SWITCHED_MODEL_OK")
    request = await model.request()
    assert request.model == selected
    await model.reply(request, Text("SWITCHED_MODEL_OK"))
    changed = await attached.until(events.is_kind("model_changed"))
    assert changed.model_changed.model == selected
    started = await attached.until(events.is_kind("turn_started"))
    assert started.turn_started.model == selected
    await attached.until(events.turn_completed)
    await attached.detach()
    await attached.drain_until_end()


@pytest.mark.parametrize("harness", [pb.HARNESS_CLAUDE])
async def test_claude_active_turn_model_command_has_a_native_causal_outcome(
    client: RunnerClient, model: ScriptedModel, spec: pb.SessionSpec
) -> None:
    """Claude's set-model control request can settle while a model request is still active."""
    session = await client.attach("switch-model-active-1", spec=spec)
    await session.send("input-1", "Wait; do not answer early.")
    request = await model.request()
    await model.hold(request)
    confirmed = await session.until(events.is_kind("harness_user_message_confirmed"))
    selected = "agentplane-switched/claude-haiku-4-5-20251001"
    await session.switch_model("switch-active", selected)
    received = await session.until(events.is_kind("command_received"))
    assert received.command_received.command_id == "switch-active"

    await session.interrupt("interrupt-active", confirmed.harness_user_message_confirmed.turn_id)
    interrupted = await session.until(events.turn_completed)
    assert interrupted.turn_completed.status == pb.TURN_STATUS_INTERRUPTED

    def is_outcome(event: pb.Event) -> bool:
        return (events.kind(event) == "model_changed" and event.model_changed.command_id == "switch-active") or (
            events.kind(event) == "command_rejected" and event.command_rejected.command_id == "switch-active"
        )

    seen = [event for event in session.seen if is_outcome(event)]
    outcome = seen[-1] if seen else await session.until(is_outcome)
    assert events.kind(outcome) in {"model_changed", "command_rejected"}

    await session.detach()
    await session.drain_until_end()


@pytest.mark.parametrize("harness", [pb.HARNESS_CODEX])
async def test_codex_active_turn_model_command_takes_effect_on_its_next_turn_start(
    client: RunnerClient, model: ScriptedModel, spec: pb.SessionSpec
) -> None:
    """Codex cannot select a model until its later turn/start response proves that selection."""
    session = await client.attach("switch-model-active-1", spec=spec)
    await session.send("input-1", "Wait; do not answer early.")
    request = await model.request()
    await model.hold(request)
    confirmed = await session.until(events.is_kind("harness_user_message_confirmed"))
    selected = "agentplane-switched-model"
    await session.switch_model("switch-active", selected)
    received = await session.until(events.is_kind("command_received"))
    assert received.command_received.command_id == "switch-active"

    await session.interrupt("interrupt-active", confirmed.harness_user_message_confirmed.turn_id)
    interrupted = await session.until(events.turn_completed)
    assert interrupted.turn_completed.status == pb.TURN_STATUS_INTERRUPTED

    await session.send("input-2", "Reply with exactly: ACTIVE_MODEL_OK")
    request = await model.request()
    assert request.model == selected
    await model.reply(request, Text("ACTIVE_MODEL_OK"))
    changed = await session.until(events.is_kind("model_changed"))
    assert changed.model_changed.command_id == "switch-active"
    assert changed.model_changed.model == selected
    await session.until(events.turn_completed)

    await session.detach()
    await session.drain_until_end()


async def test_interrupt_ends_the_turn_as_interrupted(
    client: RunnerClient, model: ScriptedModel, spec: pb.SessionSpec
) -> None:
    session = await client.attach("interrupt-1", spec=spec)
    await session.send("input-1", "Wait; do not answer early.")

    request = await model.request()
    await model.hold(request)
    confirmed = await session.until(events.is_kind("harness_user_message_confirmed"))
    await session.interrupt("interrupt-1", confirmed.harness_user_message_confirmed.turn_id)

    done = await session.until(events.turn_completed)
    assert done.turn_completed.status == pb.TURN_STATUS_INTERRUPTED
    assert done.turn_completed.interrupted_by_command_id == "interrupt-1"
    assert not events.of_kind(session.seen, "item_completed")

    await session.send("input-2", "Reply with exactly: AFTER_INTERRUPT_OK")
    request = await model.request()
    assert request.user_texts[-1] == "Reply with exactly: AFTER_INTERRUPT_OK"
    await model.reply(request, Text("AFTER_INTERRUPT_OK"))
    done = await session.until(events.turn_completed)
    assert done.turn_completed.status == pb.TURN_STATUS_COMPLETED
    await session.detach()
    await session.drain_until_end()


if __name__ == "__main__":
    pytest_bazel.main()
