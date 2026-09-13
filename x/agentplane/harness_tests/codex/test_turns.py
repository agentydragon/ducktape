"""Plain turns: one scripted exchange, and native thread resume across processes."""

from __future__ import annotations

import pytest_bazel

from x.agentplane.harness_tests.codex import frames, responses_sse as sse
from x.agentplane.harness_tests.codex.harness import EFFORT, MODEL, CodexHarness
from x.agentplane.harness_tests.codex.requests import ResponsesRequest
from x.agentplane.harness_tests.scripted_upstream import ScriptedUpstream
from x.agentplane.native.codex import driver, scenarios, wire

TOOLS = ["exec_command", "write_stdin", "request_user_input"]
IN_FLIGHT_INPUT = "Reply with exactly: CODEX_CRASHED_IN_FLIGHT_REPLAYED"
QUEUED_INPUT = "Reply with exactly: CODEX_CRASHED_QUEUE_FATE"
RECOVERY_INPUT = "Reply with exactly: CODEX_CRASH_RESUME_OK"


def test_baseline_turn(codex: CodexHarness, upstream: ScriptedUpstream) -> None:
    with codex.start(upstream) as process:
        thread_id = scenarios.launch_handshake(process, cwd=str(codex.workspace), model=MODEL, effort=EFFORT)[
            "thread_id"
        ]
        turn_id = scenarios.start_turn(
            process, thread_id=thread_id, request_id="capture-3", text="Reply with exactly: CAPTURE_BASELINE_OK"
        )

        raw = upstream.next_request()
        request = ResponsesRequest.parse(raw)
        assert raw.path == "/v1/responses"
        assert request.model == MODEL
        assert request.stream is True
        assert request.instructions == driver.BASE_INSTRUCTIONS
        assert request.tool_names == TOOLS
        assert request.item_kinds == ["message:user"]
        assert request.messages("user")[-1].text == "Reply with exactly: CAPTURE_BASELINE_OK"
        assert request.prompt_cache_key == thread_id
        assert request.client_metadata.thread_id == thread_id
        assert request.client_metadata.turn_id == turn_id
        for section in ("<skills_instructions>", "<permissions instructions>", "<apps_instructions>"):
            assert section not in raw.body.decode()
        upstream.respond(
            raw,
            sse.response_stream(
                [sse.Reasoning("brief", "enc_test_1"), sse.Message("CAPTURE_BASELINE_OK")], model=MODEL
            ),
        )

        terminal = scenarios.await_turn_completed(process)
        assert terminal["params"]["turn"]["status"] == "completed"
        assert process.alive()
    captured = process.stdout_frames()
    frames.assert_success(captured, "CAPTURE_BASELINE_OK")
    frames.assert_item_lifecycles(captured, wire.UserMessageItem)
    assert len(upstream.observed) == 1
    upstream.assert_quiescent()


def test_idle_resume_replays_the_thread_from_disk(codex: CodexHarness, upstream: ScriptedUpstream) -> None:
    with codex.start(upstream) as first:
        thread_id = scenarios.launch_handshake(
            first, cwd=str(codex.workspace), model=MODEL, effort=EFFORT, persist=True
        )["thread_id"]
        scenarios.start_turn(
            first, thread_id=thread_id, request_id="capture-3", text="Reply with exactly: IDLE_RESUME_SEED_OK"
        )
        raw = upstream.next_request()
        upstream.respond(
            raw,
            sse.response_stream([sse.Reasoning("seed", "enc_test_1"), sse.Message("IDLE_RESUME_SEED_OK")], model=MODEL),
        )
        assert scenarios.await_turn_completed(first)["params"]["turn"]["status"] == "completed"

    with codex.start(upstream) as second:
        resumed = scenarios.resume_handshake(second, thread_id=thread_id)
        assert resumed["thread_resume_response"]["result"]["thread"]["id"] == thread_id
        scenarios.start_turn(
            second, thread_id=thread_id, request_id="capture-6", text="Reply with exactly: IDLE_RESUME_OK"
        )

        raw = upstream.next_request()
        request = ResponsesRequest.parse(raw)
        assert request.item_kinds == ["message:user", "reasoning", "message:assistant", "message:user"]
        assert [message.text for message in request.messages("user")] == [
            "Reply with exactly: IDLE_RESUME_SEED_OK",
            "Reply with exactly: IDLE_RESUME_OK",
        ]
        assert request.messages("assistant")[0].text == "IDLE_RESUME_SEED_OK"
        assert request.reasoning[0].encrypted_content == "enc_test_1"
        assert request.prompt_cache_key == thread_id
        upstream.respond(raw, sse.response_stream([sse.Message("IDLE_RESUME_OK")], model=MODEL))
        assert scenarios.await_turn_completed(second)["params"]["turn"]["status"] == "completed"
    frames.assert_success(second.stdout_frames(), "IDLE_RESUME_OK")
    upstream.assert_quiescent()


def test_resume_after_crash_replays_the_in_flight_turn_but_not_its_live_followup(
    codex: CodexHarness, upstream: ScriptedUpstream
) -> None:
    """A persisted thread survives a killed app-server; its active-turn input queue does not."""
    with codex.start(upstream) as first:
        thread_id = scenarios.launch_handshake(
            first, cwd=str(codex.workspace), model=MODEL, effort=EFFORT, persist=True
        )["thread_id"]
        turn_id = scenarios.start_turn(first, thread_id=thread_id, request_id="crash-3", text=IN_FLIGHT_INPUT)
        scenarios.await_turn_started(first)
        raw = upstream.next_request()

        first.write(driver.turn_start("crash-4", thread_id=thread_id, text=QUEUED_INPUT))
        joined = first.await_frame(lambda item: item.get("id") == "crash-4", timeout=30)
        assert joined["result"]["turn"]["id"] == turn_id
        assert first.crash() < 0
    assert raw.client_closed.wait(30)

    with codex.start(upstream) as resumed:
        assert scenarios.resume_handshake(resumed, thread_id=thread_id)["thread_id"] == thread_id
        scenarios.start_turn(resumed, thread_id=thread_id, request_id="crash-6", text=RECOVERY_INPUT)
        replay = ResponsesRequest.parse(upstream.next_request())
        assert [message.text for message in replay.messages("user")] == [IN_FLIGHT_INPUT, RECOVERY_INPUT]
        assert QUEUED_INPUT not in [message.text for message in replay.messages("user")]
        assert replay.messages("assistant") == []
        upstream.respond(
            upstream.observed[-1], sse.response_stream([sse.Message("CODEX_CRASH_RESUME_OK")], model=MODEL)
        )
        assert scenarios.await_turn_completed(resumed)["params"]["turn"]["status"] == "completed"
    upstream.assert_quiescent()


if __name__ == "__main__":
    pytest_bazel.main()
