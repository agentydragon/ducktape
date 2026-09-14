"""Input and control while a turn is active: queued input delivery and interruption."""

from __future__ import annotations

import pytest_bazel

from x.agentplane.harness_tests.claude import anthropic_sse as sse, frames
from x.agentplane.harness_tests.claude.harness import MODEL, ClaudeHarness
from x.agentplane.harness_tests.claude.messages import AnthropicMessages
from x.agentplane.native.claude import wire

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
    async with claude.start(anthropic_messages, replay_user_messages=True) as run:
        initial = await run.send("Finish this first turn before taking later messages.")
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
            await initial.active()
            first, second, third = await run.send_many([COALESCED_FIRST, COALESCED_SECOND, COALESCED_THIRD])
            for prompt in (first, second, third):
                await prompt.lifecycle(wire.CommandState.QUEUED)
            await initial_exchange.send(*initial_stream.events[content_started + 1 :])
            await initial_exchange.close()
            assert (await initial.result()).result == "INITIAL_TURN_DONE"

        async with await anthropic_messages.await_next_request() as exchange:
            request = exchange.request
            coalesced = f"{COALESCED_FIRST}\n{COALESCED_SECOND}\n{COALESCED_THIRD}"
            assert request.last_message.role == "user"
            assert request.texts("user")[-1] == coalesced
            assert [text for text in request.texts("user") if "COALESCED_" in text] == [coalesced]
            assert request.texts("assistant") == ["INITIAL_TURN_DONE"]
            stream = sse.message_stream([sse.Text("COALESCED_OK")], model=MODEL)
            events = run.events()
            await exchange.send(*stream.events)
            await exchange.close()
            assert (await events.result()).result == "COALESCED_OK"

    parsed = [wire.parse_frame(frame) for frame in run.native_frames()]
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
    async with claude.start(anthropic_messages, replay_user_messages=True) as run:
        initial = await run.send("Keep this first turn active until interrupted.")
        async with await anthropic_messages.await_next_request() as initial_exchange:
            stream = sse.message_stream([sse.Text("never finished")], model=MODEL)
            await initial_exchange.send(*stream.through("content_block_start").events)
            await initial.active()

            first, second = await run.send_many([INTERRUPTED_QUEUE_FIRST, INTERRUPTED_QUEUE_SECOND])
            for prompt in (first, second):
                await prompt.lifecycle(wire.CommandState.QUEUED)

            # Claude emits each cancellation before acknowledging the interrupt control request.
            # The run waits for the later reply without consuming the two cancellation receipts:
            # each input holds its own cursor from before it entered the native queue.
            assert (await run.interrupt(cancel_queued=True)).response.subtype == "success"
            for prompt in (first, second):
                await prompt.lifecycle(wire.CommandState.CANCELLED)
            await initial_exchange.wait_client_closed()
            assert (await initial.result()).is_error is True

        recovery = await run.send(INTERRUPT_RECOVERY)
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
            await recovery_exchange.close()
            assert (await recovery.result()).result == "INTERRUPT_QUEUE_RECOVERY_OK"

    cancelled = [
        frame.command_uuid
        for frame in (wire.parse_frame(raw) for raw in run.native_frames())
        if isinstance(frame, wire.CommandLifecycleFrame) and frame.state is wire.CommandState.CANCELLED
    ]
    assert cancelled.count(first.uuid) == 1
    assert cancelled.count(second.uuid) == 1


async def test_set_model_during_an_active_turn_controls_the_next_model_request(
    claude: ClaudeHarness, anthropic_messages: AnthropicMessages
) -> None:
    async with claude.start(anthropic_messages) as run:
        initial = await run.send("Wait; do not answer early.")

        async with await anthropic_messages.await_next_request() as exchange:
            stream = sse.message_stream([sse.Text("never finished")], model=MODEL)
            await exchange.send(*stream.through("content_block_start").events)
            await initial.active()

            assert (await run.set_model(SELECTED_MODEL)).response.subtype == "success"

            await run.interrupt(cancel_queued=False)
            await exchange.wait_client_closed()
            assert (await initial.result()).is_error is True

        prompt = await run.send("Reply with exactly: SELECTED_MODEL_OK")
        async with await anthropic_messages.await_next_request() as next_exchange:
            assert next_exchange.request.model == SELECTED_MODEL
            stream = sse.message_stream([sse.Text("SELECTED_MODEL_OK")], model=SELECTED_MODEL)
            await next_exchange.send(*stream.events)
        assert (await prompt.result()).result == "SELECTED_MODEL_OK"


async def test_inputs_during_a_tool_coalesce_into_the_tool_result(
    claude: ClaudeHarness, anthropic_messages: AnthropicMessages
) -> None:
    """Claude has no steering frame: active-turn inputs join the current tool result as one
    newline-joined native cohort rather than becoming their own user messages."""
    # The runner enables replay so normal queued batches can retain every origin. Pin the
    # active-tool behavior under that same production flag: it has a different output shape from
    # the ordinary non-replay CLI path.
    async with claude.start(anthropic_messages, replay_user_messages=True) as run:
        first = await run.send("Wait with the shell, then reply WAIT_DONE.")

        async with await anthropic_messages.await_next_request() as exchange:
            stream = sse.message_stream([sse.ToolUse("toolu_test_1", "Bash", {"command": WAIT_COMMAND})], model=MODEL)
            await exchange.send(*stream.events)
        active = await first.active()
        assert isinstance(active, wire.StreamEventFrame)
        second = await run.send(SECOND_INPUT)
        third = await run.send(THIRD_INPUT)
        assert first.uuid != second.uuid
        assert second.uuid != third.uuid

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

        assert (await first.result()).result == "SECOND_INPUT_OBSERVED"
        assert run.running
    frames.assert_success(run.native_frames(), "SECOND_INPUT_OBSERVED")
    parsed = [wire.parse_frame(frame) for frame in run.native_frames()]
    starts = [
        frame
        for frame in parsed
        if isinstance(frame, wire.StreamEventFrame) and isinstance(frame.event, wire.MessageStart)
    ]
    # Normal prompts carry their input UUID into the model request. The active-tool continuation
    # deliberately omits it, leaving only the following lifecycle started cohort to identify the
    # queued inputs that were folded into its tool result.
    assert [frame.user_message_uuid for frame in starts] == [first.uuid, None]
    started = [
        frame.command_uuid
        for frame in parsed
        if isinstance(frame, wire.CommandLifecycleFrame) and frame.state is wire.CommandState.STARTED
    ]
    assert started == [first.uuid, second.uuid, third.uuid]
    # Replay preserves only the follower's original text. It is bookkeeping before both lifecycle
    # starts, not a second native user message or an echo of the text Claude put into the model
    # continuation.
    assert [
        (frame.uuid, frame.message.content)
        for frame in parsed
        if isinstance(frame, wire.UserFrame) and frame.is_replay and frame.message.content == SECOND_INPUT
    ] == [(second.uuid, SECOND_INPUT)]


async def test_interrupt_aborts_the_in_flight_model_call(
    claude: ClaudeHarness, anthropic_messages: AnthropicMessages
) -> None:
    async with claude.start(anthropic_messages) as run:
        prompt = await run.send("Wait with the shell; do not answer early.")

        async with await anthropic_messages.await_next_request() as exchange:
            stream = sse.message_stream([sse.Text("never finished")], model=MODEL)
            await exchange.send(*stream.through("content_block_start").events)
            await prompt.active()

            assert (await run.interrupt(cancel_queued=False)).response.subtype == "success"
            await exchange.wait_client_closed()
            assert (await prompt.result()).is_error is True
            assert run.running
    captured = run.native_frames()
    frames.assert_failure(frames.terminals(captured)[-1], result_fragment="", terminal_reason="aborted_streaming")
    assert not frames.tool_uses(captured)


if __name__ == "__main__":
    pytest_bazel.main()
