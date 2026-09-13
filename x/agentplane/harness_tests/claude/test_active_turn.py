"""Input and control while a turn is active: queued input delivery and interruption."""

from __future__ import annotations

from typing import Any

import pytest_bazel

from x.agentplane.harness_tests.claude import anthropic_sse as sse, frames
from x.agentplane.harness_tests.claude.harness import MODEL, ClaudeHarness
from x.agentplane.harness_tests.claude.requests import MessagesRequest
from x.agentplane.harness_tests.scripted_upstream import ScriptedUpstream, Stream
from x.agentplane.native.claude import driver, scenarios, wire

WAIT_COMMAND = 'sh -c \'printf "wait_started\\n"; sleep 3; printf "wait_finished\\n"\''
SECOND_INPUT = "Reply ONLY SECOND_INPUT_OBSERVED after your current work."
THIRD_INPUT = "Reply ONLY THIRD_INPUT_OBSERVED after your current work."
COALESCED_FIRST = "Reply only after seeing COALESCED_FIRST."
COALESCED_SECOND = "Reply only after seeing COALESCED_SECOND."
COALESCED_THIRD = "Reply only after seeing COALESCED_THIRD."
INTERRUPTED_QUEUE_FIRST = "Reply only after seeing INTERRUPTED_QUEUE_FIRST."
INTERRUPTED_QUEUE_SECOND = "Reply only after seeing INTERRUPTED_QUEUE_SECOND."
INTERRUPT_RECOVERY = "Reply with exactly: INTERRUPT_QUEUE_RECOVERY_OK"
SELECTED_MODEL = "agentplane-switched/claude-haiku-4-5-20251001"


def test_queued_inputs_coalesce_into_one_native_user_message(claude: ClaudeHarness, upstream: ScriptedUpstream) -> None:
    """Claude batches compatible queued prompts with newlines, retaining the last frame UUID.

    With replay enabled it also emits a synthetic echo for each batch follower before the merged
    native user message. The command-lifecycle frames remain per submitted input. The runner must
    therefore preserve both origins on the latter, not mistake the synthetic echo for delivery.
    """
    with claude.start(upstream, replay_user_messages=True) as process:
        scenarios.launch_handshake(process)
        scenarios.send(process, "Finish this first turn before taking later messages.")
        initial_raw = upstream.next_request()
        initial_stream = sse.message_stream([sse.Text("INITIAL_TURN_DONE")], model=MODEL)
        content_started = next(
            index for index, packet in enumerate(initial_stream.packets) if packet.kind == "content_block_start"
        )
        # Keep the first query active while later messages enter the native queue, then let that
        # turn finish. This is the headless driver's actual batching boundary: direct stdin
        # writes before a turn begins are drained one-at-a-time by the input reader.
        initial_request = MessagesRequest.parse(initial_raw)
        assert initial_request.last_message.role == "user"
        assert initial_request.last_message.content == "Finish this first turn before taking later messages."
        upstream.respond(initial_raw, Stream(initial_stream.packets[: content_started + 1]).held())
        scenarios.await_active(process)
        first = driver.user_frame(COALESCED_FIRST)
        second = driver.user_frame(COALESCED_SECOND)
        third = driver.user_frame(COALESCED_THIRD)
        process.write_many([first, second, third])
        for command_uuid in (first.uuid, second.uuid, third.uuid):

            def is_queued(frame: dict[str, Any], expected: str = command_uuid) -> bool:
                return (
                    frame.get("type") == "command_lifecycle"
                    and frame.get("command_uuid") == expected
                    and frame.get("state") == "queued"
                )

            process.await_frame(is_queued, timeout=30)
        upstream.respond(initial_raw, Stream(initial_stream.packets[content_started + 1 :]))
        assert scenarios.await_result(process)["result"] == "INITIAL_TURN_DONE"

        raw = upstream.next_request()
        request = MessagesRequest.parse(raw)
        coalesced = f"{COALESCED_FIRST}\n{COALESCED_SECOND}\n{COALESCED_THIRD}"
        assert request.last_message.role == "user"
        assert request.last_message.content == coalesced
        assert [text for text in request.texts("user") if "COALESCED_" in text] == [coalesced]
        assert request.texts("assistant") == ["INITIAL_TURN_DONE"]
        upstream.respond(raw, sse.message_stream([sse.Text("COALESCED_OK")], model=MODEL))
        assert scenarios.await_result(process)["result"] == "COALESCED_OK"

    parsed = [wire.parse_frame(frame) for frame in process.stdout_frames()]
    queued = [
        frame.command_uuid
        for frame in parsed
        if isinstance(frame, wire.CommandLifecycleFrame) and frame.state is wire.CommandState.QUEUED
    ]
    assert queued[-3:] == [first.uuid, second.uuid, third.uuid]
    follower_and_merged = [
        (frame.uuid, frame.message.content)
        for frame in parsed
        if isinstance(frame, wire.UserFrame)
        and frame.is_replay
        and frame.message.content
        in {
            COALESCED_FIRST,
            f"{COALESCED_FIRST}\n{COALESCED_SECOND}",
            f"{COALESCED_FIRST}\n{COALESCED_SECOND}\n{COALESCED_THIRD}",
        }
    ]
    assert follower_and_merged == [
        (first.uuid, COALESCED_FIRST),
        (second.uuid, f"{COALESCED_FIRST}\n{COALESCED_SECOND}"),
        (third.uuid, f"{COALESCED_FIRST}\n{COALESCED_SECOND}\n{COALESCED_THIRD}"),
    ]
    replayed = [
        frame
        for frame in parsed
        if isinstance(frame, wire.UserFrame) and frame.is_replay and frame.message.content == coalesced
    ]
    assert len(replayed) == 1
    assert replayed[0].uuid == third.uuid
    assert replayed[0].message.content == coalesced
    upstream.assert_quiescent()


def test_interrupt_cancels_each_queued_input_before_native_message(
    claude: ClaudeHarness, upstream: ScriptedUpstream
) -> None:
    """An interrupt with cancel_queued drops every queued input before it reaches the model.

    This pins the native per-input cancellation frames and the absence of both texts from the
    subsequent model request. The runner can therefore settle every originating command as a
    no-op instead of leaving a durable receipt permanently pending.
    """
    with claude.start(upstream, replay_user_messages=True) as process:
        scenarios.launch_handshake(process)
        scenarios.send(process, "Keep this first turn active until interrupted.")
        initial_raw = upstream.next_request()
        stream = sse.message_stream([sse.Text("never finished")], model=MODEL)
        upstream.respond(initial_raw, stream.until("content_block_start").held())
        scenarios.await_active(process)

        first = driver.user_frame(INTERRUPTED_QUEUE_FIRST)
        second = driver.user_frame(INTERRUPTED_QUEUE_SECOND)
        process.write_many([first, second])
        for command_uuid in (first.uuid, second.uuid):

            def is_queued(frame: dict[str, Any], expected: str = command_uuid) -> bool:
                return (
                    frame.get("type") == "command_lifecycle"
                    and frame.get("command_uuid") == expected
                    and frame.get("state") == "queued"
                )

            process.await_frame(is_queued, timeout=30)

        response = scenarios.interrupt(process, cancel_queued=True)
        assert response["response"]["subtype"] == "success"
        assert initial_raw.client_closed.wait(30)
        assert scenarios.await_result(process)["is_error"] is True

        scenarios.send(process, INTERRUPT_RECOVERY)
        recovery_raw = upstream.next_request()
        request = MessagesRequest.parse(recovery_raw)
        assert request.last_message.role == "user"
        assert request.last_message.content == INTERRUPT_RECOVERY
        assert all(
            marker not in text
            for marker in (INTERRUPTED_QUEUE_FIRST, INTERRUPTED_QUEUE_SECOND)
            for text in request.texts("user")
        )
        upstream.respond(recovery_raw, sse.message_stream([sse.Text("INTERRUPT_QUEUE_RECOVERY_OK")], model=MODEL))
        assert scenarios.await_result(process)["result"] == "INTERRUPT_QUEUE_RECOVERY_OK"

    cancelled = [
        frame.command_uuid
        for frame in (wire.parse_frame(raw) for raw in process.stdout_frames())
        if isinstance(frame, wire.CommandLifecycleFrame) and frame.state is wire.CommandState.CANCELLED
    ]
    assert cancelled[-2:] == [first.uuid, second.uuid]
    upstream.assert_quiescent()


def test_set_model_during_an_active_turn_controls_the_next_model_request(
    claude: ClaudeHarness, upstream: ScriptedUpstream
) -> None:
    with claude.start(upstream) as process:
        scenarios.launch_handshake(process)
        scenarios.send(process, "Wait; do not answer early.")

        raw = upstream.next_request()
        stream = sse.message_stream([sse.Text("never finished")], model=MODEL)
        upstream.respond(raw, stream.until("content_block_start").held())
        scenarios.await_active(process)

        change = driver.set_model(SELECTED_MODEL)
        process.write(change)
        response = process.await_frame(
            lambda item: (
                item.get("type") == "control_response"
                and item.get("response", {}).get("request_id") == change.request_id
            ),
            timeout=30,
        )
        assert response["response"]["subtype"] == "success"

        scenarios.interrupt(process, cancel_queued=False)
        assert raw.client_closed.wait(30)
        assert scenarios.await_result(process)["is_error"] is True

        scenarios.send(process, "Reply with exactly: SELECTED_MODEL_OK")
        next_raw = upstream.next_request()
        assert MessagesRequest.parse(next_raw).model == SELECTED_MODEL
        upstream.respond(next_raw, sse.message_stream([sse.Text("SELECTED_MODEL_OK")], model=SELECTED_MODEL))
        assert scenarios.await_result(process)["result"] == "SELECTED_MODEL_OK"
    upstream.assert_quiescent()


def test_inputs_during_a_tool_coalesce_into_the_tool_result(claude: ClaudeHarness, upstream: ScriptedUpstream) -> None:
    """Claude has no steering frame: active-turn inputs join the current tool result as one
    newline-joined native cohort rather than becoming their own user messages."""
    # The runner enables replay so normal queued batches can retain every origin. Pin the
    # active-tool behavior under that same production flag: it has a different output shape from
    # the ordinary non-replay CLI path.
    with claude.start(upstream, replay_user_messages=True) as process:
        scenarios.launch_handshake(process)
        first_uuid = scenarios.send(process, "Wait with the shell, then reply WAIT_DONE.")

        raw = upstream.next_request()
        upstream.respond(
            raw, sse.message_stream([sse.ToolUse("toolu_test_1", "Bash", {"command": WAIT_COMMAND})], model=MODEL)
        )
        active = scenarios.await_active(process)
        assert active["type"] == "stream_event"
        second_uuid = scenarios.send(process, SECOND_INPUT)
        third_uuid = scenarios.send(process, THIRD_INPUT)
        assert first_uuid != second_uuid
        assert second_uuid != third_uuid

        raw = upstream.next_request()
        request = MessagesRequest.parse(raw)
        (result,) = request.tool_results
        assert result.tool_use_id == "toolu_test_1"
        assert "wait_started\nwait_finished\n" in result.text
        assert SECOND_INPUT in result.text
        assert THIRD_INPUT in result.text
        assert SECOND_INPUT not in request.texts("user")
        upstream.respond(raw, sse.message_stream([sse.Text("SECOND_INPUT_OBSERVED")], model=MODEL))

        assert scenarios.await_result(process)["result"] == "SECOND_INPUT_OBSERVED"
        assert process.alive()
    frames.assert_success(process.stdout_frames(), "SECOND_INPUT_OBSERVED")
    parsed = [wire.parse_frame(frame) for frame in process.stdout_frames()]
    starts = [
        frame
        for frame in parsed
        if isinstance(frame, wire.StreamEventFrame) and isinstance(frame.event, wire.MessageStart)
    ]
    # Normal prompts carry their input UUID into the model request. The active-tool continuation
    # deliberately omits it, leaving only the following lifecycle started cohort to identify the
    # queued inputs that were folded into its tool result.
    assert [frame.user_message_uuid for frame in starts] == [first_uuid, None]
    started = [
        frame.command_uuid
        for frame in parsed
        if isinstance(frame, wire.CommandLifecycleFrame) and frame.state is wire.CommandState.STARTED
    ]
    assert started == [first_uuid, second_uuid, third_uuid]
    # Replay preserves only the follower's original text. It is bookkeeping before both lifecycle
    # starts, not a second native user message or an echo of the text Claude put into the model
    # continuation.
    assert [
        (frame.uuid, frame.message.content)
        for frame in parsed
        if isinstance(frame, wire.UserFrame) and frame.is_replay and frame.message.content == SECOND_INPUT
    ] == [(second_uuid, SECOND_INPUT)]
    upstream.assert_quiescent()


def test_interrupt_aborts_the_in_flight_model_call(claude: ClaudeHarness, upstream: ScriptedUpstream) -> None:
    with claude.start(upstream) as process:
        scenarios.launch_handshake(process)
        scenarios.send(process, "Wait with the shell; do not answer early.")

        raw = upstream.next_request()
        stream = sse.message_stream([sse.Text("never finished")], model=MODEL)
        upstream.respond(raw, stream.until("content_block_start").held())
        scenarios.await_active(process)

        response = scenarios.interrupt(process, cancel_queued=False)
        assert response["response"]["subtype"] == "success"
        assert raw.client_closed.wait(30)
        result = scenarios.await_result(process)
        assert result["is_error"] is True
        assert process.alive()
    captured = process.stdout_frames()
    frames.assert_failure(frames.terminals(captured)[-1], result_fragment="", terminal_reason="aborted_streaming")
    assert not frames.tool_uses(captured)
    upstream.assert_quiescent()


if __name__ == "__main__":
    pytest_bazel.main()
