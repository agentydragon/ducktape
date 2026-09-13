"""Input and control while a turn is active: queued input delivery and interruption."""

from __future__ import annotations

from typing import Any

import pytest_bazel

from x.agentplane.harness_tests.claude import anthropic_sse as sse, frames
from x.agentplane.harness_tests.claude.harness import MODEL, ClaudeHarness
from x.agentplane.harness_tests.claude.messages import AnthropicMessages
from x.agentplane.native.claude import async_scenarios as scenarios, driver, wire

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


async def test_queued_inputs_coalesce_into_one_native_user_message(
    claude: ClaudeHarness, anthropic_messages: AnthropicMessages
) -> None:
    """Claude batches compatible queued prompts with newlines, retaining the last frame UUID.

    With replay enabled it emits a synthetic echo for the batch leader and the final merged native
    user message. The command-lifecycle frames remain per submitted input. The runner must
    therefore preserve every origin from those lifecycle frames, not mistake the synthetic echo
    for delivery.
    """
    async with claude.start(anthropic_messages, replay_user_messages=True) as process:
        await scenarios.launch_handshake(process)
        await scenarios.send(process, "Finish this first turn before taking later messages.")
        async with await anthropic_messages.await_next_request() as initial_exchange:
            initial_stream = sse.message_stream([sse.Text("INITIAL_TURN_DONE")], model=MODEL)
            content_started = next(
                index for index, event in enumerate(initial_stream.events) if event.kind == "content_block_start"
            )
            # Keep the first query active while later messages enter the native queue, then let that
            # turn finish. This is the headless driver's actual batching boundary: direct stdin
            # writes before a turn begins are drained one-at-a-time by the input reader.
            initial_request = initial_exchange.request
            assert initial_request.last_message.role == "user"
            assert initial_request.texts("user")[-1] == "Finish this first turn before taking later messages."
            await initial_exchange.send(*initial_stream.events[: content_started + 1])
            await scenarios.await_active(process)
            first = driver.user_frame(COALESCED_FIRST)
            second = driver.user_frame(COALESCED_SECOND)
            third = driver.user_frame(COALESCED_THIRD)
            await process.send_many([first, second, third])
            for command_uuid in (first.uuid, second.uuid, third.uuid):

                def is_queued(frame: dict[str, Any], expected: str = command_uuid) -> bool:
                    return (
                        frame.get("type") == "command_lifecycle"
                        and frame.get("command_uuid") == expected
                        and frame.get("state") == "queued"
                    )

                while not is_queued(await process.next_frame()):
                    pass
            await initial_exchange.send(*initial_stream.events[content_started + 1 :])
            assert (await scenarios.await_result(process))["result"] == "INITIAL_TURN_DONE"

        async with await anthropic_messages.await_next_request() as exchange:
            request = exchange.request
            coalesced = f"{COALESCED_FIRST}\n{COALESCED_SECOND}\n{COALESCED_THIRD}"
            assert request.last_message.role == "user"
            assert request.texts("user")[-1] == coalesced
            assert [text for text in request.texts("user") if "COALESCED_" in text] == [coalesced]
            assert request.texts("assistant") == ["INITIAL_TURN_DONE"]
            stream = sse.message_stream([sse.Text("COALESCED_OK")], model=MODEL)
            await exchange.send(*stream.events)
            assert (await scenarios.await_result(process))["result"] == "COALESCED_OK"

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


async def test_interrupt_cancels_each_queued_input_before_native_message(
    claude: ClaudeHarness, anthropic_messages: AnthropicMessages
) -> None:
    """An interrupt with cancel_queued drops every queued input before it reaches the model.

    This pins the native per-input cancellation frames and the absence of both texts from the
    subsequent model request. The runner can therefore settle every originating command as a
    no-op instead of leaving a durable receipt permanently pending.
    """
    async with claude.start(anthropic_messages, replay_user_messages=True) as process:
        await scenarios.launch_handshake(process)
        await scenarios.send(process, "Keep this first turn active until interrupted.")
        async with await anthropic_messages.await_next_request() as initial_exchange:
            stream = sse.message_stream([sse.Text("never finished")], model=MODEL)
            await initial_exchange.send(*stream.through("content_block_start").events)
            await scenarios.await_active(process)

            first = driver.user_frame(INTERRUPTED_QUEUE_FIRST)
            second = driver.user_frame(INTERRUPTED_QUEUE_SECOND)
            await process.send_many([first, second])
            for command_uuid in (first.uuid, second.uuid):

                def is_queued(frame: dict[str, Any], expected: str = command_uuid) -> bool:
                    return (
                        frame.get("type") == "command_lifecycle"
                        and frame.get("command_uuid") == expected
                        and frame.get("state") == "queued"
                    )

                while not is_queued(await process.next_frame()):
                    pass

            response = await scenarios.interrupt(process, cancel_queued=True)
            assert response["response"]["subtype"] == "success"
            await initial_exchange.wait_client_closed()
            assert (await scenarios.await_result(process))["is_error"] is True
            for command_uuid in (first.uuid, second.uuid):

                def is_cancelled(frame: dict[str, Any], expected: str = command_uuid) -> bool:
                    return (
                        frame.get("type") == "command_lifecycle"
                        and frame.get("command_uuid") == expected
                        and frame.get("state") == "cancelled"
                    )

                while not is_cancelled(await process.next_frame()):
                    pass

        await scenarios.send(process, INTERRUPT_RECOVERY)
        async with await anthropic_messages.await_next_request() as recovery_exchange:
            request = recovery_exchange.request
            assert request.last_message.role == "user"
            assert request.texts("user")[-1] == INTERRUPT_RECOVERY
            assert all(
                marker not in text
                for marker in (INTERRUPTED_QUEUE_FIRST, INTERRUPTED_QUEUE_SECOND)
                for text in request.texts("user")
            )
            stream = sse.message_stream([sse.Text("INTERRUPT_QUEUE_RECOVERY_OK")], model=MODEL)
            await recovery_exchange.send(*stream.events)
            assert (await scenarios.await_result(process))["result"] == "INTERRUPT_QUEUE_RECOVERY_OK"

    cancelled = [
        frame.command_uuid
        for frame in (wire.parse_frame(raw) for raw in process.stdout_frames())
        if isinstance(frame, wire.CommandLifecycleFrame) and frame.state is wire.CommandState.CANCELLED
    ]
    assert cancelled.count(first.uuid) == 1
    assert cancelled.count(second.uuid) == 1


async def test_set_model_during_an_active_turn_controls_the_next_model_request(
    claude: ClaudeHarness, anthropic_messages: AnthropicMessages
) -> None:
    async with claude.start(anthropic_messages) as process:
        await scenarios.launch_handshake(process)
        await scenarios.send(process, "Wait; do not answer early.")

        async with await anthropic_messages.await_next_request() as exchange:
            stream = sse.message_stream([sse.Text("never finished")], model=MODEL)
            await exchange.send(*stream.through("content_block_start").events)
            await scenarios.await_active(process)

            change = driver.set_model(SELECTED_MODEL)
            await process.send(change)
            while True:
                response = await process.next_frame()
                if (
                    response.get("type") == "control_response"
                    and response.get("response", {}).get("request_id") == change.request_id
                ):
                    break
            assert response["response"]["subtype"] == "success"

            await scenarios.interrupt(process, cancel_queued=False)
            await exchange.wait_client_closed()
            assert (await scenarios.await_result(process))["is_error"] is True

        await scenarios.send(process, "Reply with exactly: SELECTED_MODEL_OK")
        async with await anthropic_messages.await_next_request() as next_exchange:
            assert next_exchange.request.model == SELECTED_MODEL
            stream = sse.message_stream([sse.Text("SELECTED_MODEL_OK")], model=SELECTED_MODEL)
            await next_exchange.send(*stream.events)
        assert (await scenarios.await_result(process))["result"] == "SELECTED_MODEL_OK"


async def test_inputs_during_a_tool_coalesce_into_the_tool_result(
    claude: ClaudeHarness, anthropic_messages: AnthropicMessages
) -> None:
    """Claude has no steering frame: active-turn inputs join the current tool result as one
    newline-joined native cohort rather than becoming their own user messages."""
    # The runner enables replay so normal queued batches can retain every origin. Pin the
    # active-tool behavior under that same production flag: it has a different output shape from
    # the ordinary non-replay CLI path.
    async with claude.start(anthropic_messages, replay_user_messages=True) as process:
        await scenarios.launch_handshake(process)
        first_uuid = await scenarios.send(process, "Wait with the shell, then reply WAIT_DONE.")

        async with await anthropic_messages.await_next_request() as exchange:
            stream = sse.message_stream([sse.ToolUse("toolu_test_1", "Bash", {"command": WAIT_COMMAND})], model=MODEL)
            await exchange.send(*stream.events)
        active = await scenarios.await_active(process)
        assert active["type"] == "stream_event"
        second_uuid = await scenarios.send(process, SECOND_INPUT)
        third_uuid = await scenarios.send(process, THIRD_INPUT)
        assert first_uuid != second_uuid
        assert second_uuid != third_uuid

        async with await anthropic_messages.await_next_request() as exchange:
            request = exchange.request
            (result,) = request.tool_results
            assert result.tool_use_id == "toolu_test_1"
            assert "wait_started\nwait_finished\n" in result.text
            assert SECOND_INPUT in result.text
            assert THIRD_INPUT in result.text
            assert SECOND_INPUT not in request.texts("user")
            stream = sse.message_stream([sse.Text("SECOND_INPUT_OBSERVED")], model=MODEL)
            await exchange.send(*stream.events)

        assert (await scenarios.await_result(process))["result"] == "SECOND_INPUT_OBSERVED"
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


async def test_interrupt_aborts_the_in_flight_model_call(
    claude: ClaudeHarness, anthropic_messages: AnthropicMessages
) -> None:
    async with claude.start(anthropic_messages) as process:
        await scenarios.launch_handshake(process)
        await scenarios.send(process, "Wait with the shell; do not answer early.")

        async with await anthropic_messages.await_next_request() as exchange:
            stream = sse.message_stream([sse.Text("never finished")], model=MODEL)
            await exchange.send(*stream.through("content_block_start").events)
            await scenarios.await_active(process)

            response = await scenarios.interrupt(process, cancel_queued=False)
            assert response["response"]["subtype"] == "success"
            await exchange.wait_client_closed()
            result = await scenarios.await_result(process)
            assert result["is_error"] is True
            assert process.alive()
    captured = process.stdout_frames()
    frames.assert_failure(frames.terminals(captured)[-1], result_fragment="", terminal_reason="aborted_streaming")
    assert not frames.tool_uses(captured)


if __name__ == "__main__":
    pytest_bazel.main()
