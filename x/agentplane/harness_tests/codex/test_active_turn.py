"""Input and control while a turn is active: queued input, steering, and interruption."""

from __future__ import annotations

import pytest_bazel

from x.agentplane.harness_tests.codex import frames, responses_sse as sse
from x.agentplane.harness_tests.codex.harness import EFFORT, MODEL, CodexHarness
from x.agentplane.harness_tests.codex.requests import ResponsesRequest
from x.agentplane.harness_tests.scripted_upstream import ScriptedUpstream
from x.agentplane.native.codex import driver, scenarios

WAIT_COMMAND = 'sh -c \'printf "wait_started\\n"; sleep 3; printf "wait_finished\\n"\''
SECOND_INPUT = "Reply ONLY SECOND_INPUT_OBSERVED after current work."
STEER_INPUT = "Reply ONLY STEERED after the current tool action."


def _wait_call() -> sse.FunctionCall:
    return sse.FunctionCall("call_test_1", "exec_command", {"cmd": WAIT_COMMAND})


def test_second_input_during_a_tool_joins_the_running_turn(codex: CodexHarness, upstream: ScriptedUpstream) -> None:
    with codex.start(upstream) as process:
        thread_id = scenarios.launch_handshake(process, cwd=str(codex.workspace), model=MODEL, effort=EFFORT)[
            "thread_id"
        ]
        turn_id = scenarios.start_turn(
            process, thread_id=thread_id, request_id="capture-3", text="Wait with the shell."
        )

        raw = upstream.next_request()
        upstream.respond(raw, sse.response_stream([_wait_call()], model=MODEL))
        started = process.await_frame(
            lambda item: (
                item.get("method") == "item/started" and item["params"]["item"].get("type") == "commandExecution"
            ),
            timeout=30,
        )
        assert started["params"]["turnId"] == turn_id

        process.write(driver.turn_start("capture-4", thread_id=thread_id, text=SECOND_INPUT))
        response = process.await_frame(lambda item: item.get("id") == "capture-4", timeout=30)
        # The second turn/start is accepted as input for the turn already running.
        assert response["result"]["turn"]["id"] == turn_id

        raw = upstream.next_request()
        request = ResponsesRequest.parse(raw)
        assert request.item_kinds == ["message:user", "function_call", "function_call_output", "message:user"]
        assert request.function_call_outputs[0].call_id == "call_test_1"
        assert "wait_finished" in request.function_call_outputs[0].output
        assert request.messages("user")[-1].text == SECOND_INPUT
        upstream.respond(raw, sse.response_stream([sse.Message("SECOND_INPUT_OBSERVED")], model=MODEL))

        assert scenarios.await_turn_completed(process)["params"]["turn"]["status"] == "completed"
        assert process.alive()
    captured = process.stdout_frames()
    frames.assert_success(captured, "SECOND_INPUT_OBSERVED")
    assert len(frames.assert_item_lifecycles(captured, "userMessage")) == 2
    assert len(frames.completed_turns(captured)) == 1
    upstream.assert_quiescent()


def test_steer_during_a_tool_joins_the_running_turn(codex: CodexHarness, upstream: ScriptedUpstream) -> None:
    with codex.start(upstream) as process:
        thread_id = scenarios.launch_handshake(process, cwd=str(codex.workspace), model=MODEL, effort=EFFORT)[
            "thread_id"
        ]
        turn_id = scenarios.start_turn(
            process, thread_id=thread_id, request_id="capture-3", text="Wait with the shell."
        )

        raw = upstream.next_request()
        upstream.respond(raw, sse.response_stream([_wait_call()], model=MODEL))
        process.await_frame(
            lambda item: (
                item.get("method") == "item/started" and item["params"]["item"].get("type") == "commandExecution"
            ),
            timeout=30,
        )

        response = scenarios.steer(
            process, thread_id=thread_id, turn_id=turn_id, request_id="capture-4", text=STEER_INPUT
        )
        assert response["result"]["turnId"] == turn_id

        raw = upstream.next_request()
        request = ResponsesRequest.parse(raw)
        assert request.item_kinds == ["message:user", "function_call", "function_call_output", "message:user"]
        assert request.messages("user")[-1].text == STEER_INPUT
        upstream.respond(raw, sse.response_stream([sse.Message("STEERED")], model=MODEL))

        assert scenarios.await_turn_completed(process)["params"]["turn"]["status"] == "completed"
    captured = process.stdout_frames()
    frames.assert_success(captured, "STEERED")
    assert len(frames.completed_turns(captured)) == 1
    upstream.assert_quiescent()


def test_interrupt_aborts_the_in_flight_model_call(codex: CodexHarness, upstream: ScriptedUpstream) -> None:
    with codex.start(upstream) as process:
        thread_id = scenarios.launch_handshake(process, cwd=str(codex.workspace), model=MODEL, effort=EFFORT)[
            "thread_id"
        ]
        turn_id = scenarios.start_turn(
            process, thread_id=thread_id, request_id="capture-3", text="Wait; do not answer early."
        )
        scenarios.await_turn_started(process)

        raw = upstream.next_request()
        response = scenarios.interrupt(process, thread_id=thread_id, turn_id=turn_id, request_id="capture-5")
        assert "error" not in response
        assert raw.client_closed.wait(30)

        terminal = scenarios.await_turn_completed(process)
        assert terminal["params"]["turn"]["status"] == "interrupted"
        assert process.alive()
    captured = process.stdout_frames()
    assert not frames.agent_texts(captured)
    upstream.assert_quiescent()


if __name__ == "__main__":
    pytest_bazel.main()
