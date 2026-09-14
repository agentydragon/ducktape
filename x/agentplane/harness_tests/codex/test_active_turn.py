"""Input and control while a turn is active: queued input, steering, and interruption."""

from __future__ import annotations

import pytest_bazel

from x.agentplane.harness_tests.codex import frames, responses_sse as sse
from x.agentplane.harness_tests.codex.harness import MODEL, CodexHarness
from x.agentplane.harness_tests.codex.responses import OpenAIResponses
from x.agentplane.native.codex import wire

WAIT_COMMAND = 'sh -c \'printf "wait_started\\n"; sleep 3; printf "wait_finished\\n"\''
SECOND_INPUT = "Reply ONLY SECOND_INPUT_OBSERVED after current work."
STEER_INPUT = "Reply ONLY STEERED after the current tool action."
INTERRUPTED_QUEUE_FIRST = "Reply only after seeing CODEX_INTERRUPTED_QUEUE_FIRST."
INTERRUPTED_QUEUE_SECOND = "Reply only after seeing CODEX_INTERRUPTED_QUEUE_SECOND."
INTERRUPT_RECOVERY = "Reply with exactly: CODEX_INTERRUPT_QUEUE_RECOVERY_OK"
INTERRUPTED_INITIAL = "Wait; do not answer early."
INTERRUPTED_TURN_MARKER = (
    "<turn_aborted>\n"
    "The user interrupted the previous turn on purpose. Any running unified exec processes may still be running "
    "in the background. If any tools/commands were aborted, they may have partially executed.\n"
    "</turn_aborted>"
)


def _wait_call() -> sse.FunctionCall:
    return sse.FunctionCall("call_test_1", "exec_command", {"cmd": WAIT_COMMAND})


async def test_second_input_during_a_tool_joins_the_running_turn(
    codex: CodexHarness, openai_responses: OpenAIResponses
) -> None:
    async with codex.start(openai_responses) as run:
        turn = await run.start_turn("Wait with the shell.")

        async with await openai_responses.await_next_request() as exchange:
            stream = sse.response_stream([_wait_call()], model=MODEL)
            events = run.events()
            await exchange.send(*stream.events)
        assert (await events.command_started(turn.id)).params.turn_id == turn.id

        # The second turn/start is accepted as input for the turn already running.
        assert (await run.start_turn(SECOND_INPUT)).id == turn.id

        async with await openai_responses.await_next_request() as exchange:
            request = exchange.request
            assert request.item_kinds == ["message:user", "function_call", "function_call_output", "message:user"]
            assert request.function_call_outputs[0].call_id == "call_test_1"
            assert "wait_finished" in request.function_call_outputs[0].output
            assert request.messages("user")[-1].text == SECOND_INPUT
            stream = sse.response_stream([sse.Message("SECOND_INPUT_OBSERVED")], model=MODEL)
            await exchange.send(*stream.events)

        assert (await turn.completed()).params.turn.status is wire.TurnStatus.COMPLETED
        assert run.running
    captured = run.native_frames()
    frames.assert_success(captured, "SECOND_INPUT_OBSERVED")
    assert len(frames.assert_item_lifecycles(captured, wire.UserMessageItem)) == 2
    assert len(frames.completed_turns(captured)) == 1


async def test_steer_during_a_tool_joins_the_running_turn(
    codex: CodexHarness, openai_responses: OpenAIResponses
) -> None:
    async with codex.start(openai_responses) as run:
        turn = await run.start_turn("Wait with the shell.")

        async with await openai_responses.await_next_request() as exchange:
            stream = sse.response_stream([_wait_call()], model=MODEL)
            events = run.events()
            await exchange.send(*stream.events)
        await events.command_started(turn.id)

        response = await run.steer(turn, STEER_INPUT)
        assert response.result == {"turnId": turn.id}

        async with await openai_responses.await_next_request() as exchange:
            request = exchange.request
            assert request.item_kinds == ["message:user", "function_call", "function_call_output", "message:user"]
            assert request.messages("user")[-1].text == STEER_INPUT
            stream = sse.response_stream([sse.Message("STEERED")], model=MODEL)
            await exchange.send(*stream.events)

        assert (await turn.completed()).params.turn.status is wire.TurnStatus.COMPLETED
    captured = run.native_frames()
    frames.assert_success(captured, "STEERED")
    assert len(frames.completed_turns(captured)) == 1


async def test_interrupt_aborts_the_in_flight_model_call(
    codex: CodexHarness, openai_responses: OpenAIResponses
) -> None:
    async with codex.start(openai_responses) as run:
        turn = await run.start_turn("Wait; do not answer early.")
        await turn.started()

        async with await openai_responses.await_next_request() as exchange:
            assert (await run.interrupt(turn)).error is None
            await exchange.wait_client_closed()

        assert (await turn.completed()).params.turn.status is wire.TurnStatus.INTERRUPTED
        assert run.running
    captured = run.native_frames()
    assert not frames.agent_texts(captured)


async def test_interrupt_drops_joined_inputs_before_the_next_model_request(
    codex: CodexHarness, openai_responses: OpenAIResponses
) -> None:
    """Joined active-turn inputs vanish when their turn is interrupted before model consumption."""
    async with codex.start(openai_responses) as run:
        turn = await run.start_turn(INTERRUPTED_INITIAL)
        await turn.started()
        async with await openai_responses.await_next_request() as initial_exchange:
            for text in (INTERRUPTED_QUEUE_FIRST, INTERRUPTED_QUEUE_SECOND):
                assert (await run.start_turn(text)).id == turn.id

            assert (await run.interrupt(turn)).error is None
            await initial_exchange.wait_client_closed()
            assert (await turn.completed()).params.turn.status is wire.TurnStatus.INTERRUPTED

        recovery = await run.start_turn(INTERRUPT_RECOVERY)
        async with await openai_responses.await_next_request() as recovery_exchange:
            assert recovery_exchange.request.item_kinds == ["message:user"] * 3
            assert [message.text for message in recovery_exchange.request.messages("user")] == [
                INTERRUPTED_INITIAL,
                INTERRUPTED_TURN_MARKER,
                INTERRUPT_RECOVERY,
            ]
            stream = sse.response_stream([sse.Message("CODEX_INTERRUPT_QUEUE_RECOVERY_OK")], model=MODEL)
            await recovery_exchange.send(*stream.events)
            assert (await recovery.completed()).params.turn.status is wire.TurnStatus.COMPLETED


if __name__ == "__main__":
    pytest_bazel.main()
