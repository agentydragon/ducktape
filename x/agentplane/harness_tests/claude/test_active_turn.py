"""Input and control while a turn is active: queued input delivery and interruption."""

from __future__ import annotations

import pytest_bazel

from x.agentplane.harness_tests.claude import anthropic_sse as sse, frames
from x.agentplane.harness_tests.claude.harness import MODEL, ClaudeHarness
from x.agentplane.harness_tests.claude.requests import MessagesRequest
from x.agentplane.harness_tests.scripted_upstream import ScriptedUpstream
from x.agentplane.native.claude import scenarios

WAIT_COMMAND = 'sh -c \'printf "wait_started\\n"; sleep 3; printf "wait_finished\\n"\''
SECOND_INPUT = "Reply ONLY SECOND_INPUT_OBSERVED after your current work."


def test_second_input_during_a_tool_arrives_inside_the_tool_result(
    claude: ClaudeHarness, upstream: ScriptedUpstream
) -> None:
    """Claude has no steering frame: mid-turn input is an ordinary user frame, and the harness
    hands it to the model appended to the running tool's result rather than as its own message."""
    with claude.start(upstream) as process:
        scenarios.launch_handshake(process)
        first_uuid = scenarios.send(process, "Wait with the shell, then reply WAIT_DONE.")

        raw = upstream.next_request()
        upstream.respond(
            raw, sse.message_stream([sse.ToolUse("toolu_test_1", "Bash", {"command": WAIT_COMMAND})], model=MODEL)
        )
        active = scenarios.await_active(process)
        assert active["type"] == "stream_event"
        second_uuid = scenarios.send(process, SECOND_INPUT)
        assert first_uuid != second_uuid

        raw = upstream.next_request()
        request = MessagesRequest.parse(raw)
        (result,) = request.tool_results
        assert result.tool_use_id == "toolu_test_1"
        assert "wait_started\nwait_finished\n" in result.text
        assert SECOND_INPUT in result.text
        assert SECOND_INPUT not in request.texts("user")
        upstream.respond(raw, sse.message_stream([sse.Text("SECOND_INPUT_OBSERVED")], model=MODEL))

        assert scenarios.await_result(process)["result"] == "SECOND_INPUT_OBSERVED"
        assert process.alive()
    frames.assert_success(process.stdout_frames(), "SECOND_INPUT_OBSERVED")
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
    frames.assert_failure(captured, result_fragment="", terminal_reason="aborted_streaming")
    assert not frames.tool_uses(captured)
    upstream.assert_quiescent()


if __name__ == "__main__":
    pytest_bazel.main()
