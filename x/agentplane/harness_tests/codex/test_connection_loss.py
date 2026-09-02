"""Upstream connection loss: native retry with its error notices, and retry exhaustion."""

from __future__ import annotations

import pytest_bazel

from x.agentplane.harness_tests.codex import frames, responses_sse as sse
from x.agentplane.harness_tests.codex.harness import EFFORT, MODEL, CodexHarness
from x.agentplane.harness_tests.codex.requests import ResponsesRequest
from x.agentplane.harness_tests.scripted_upstream import Refuse, ScriptedUpstream
from x.agentplane.native.codex import scenarios, wire


def test_stream_lost_before_content_is_retried(codex: CodexHarness, upstream: ScriptedUpstream) -> None:
    with codex.start(upstream) as process:
        thread_id = scenarios.launch_handshake(process, cwd=str(codex.workspace), model=MODEL, effort=EFFORT)[
            "thread_id"
        ]
        scenarios.start_turn(
            process, thread_id=thread_id, request_id="capture-3", text="Reply with exactly: CONNECTION_RETRY_OK"
        )

        raw = upstream.next_request()
        upstream.respond(raw, sse.response_stream([sse.Message("lost")], model=MODEL).until("response.created"))
        notice = process.await_frame(lambda item: item.get("method") == "error", timeout=60)
        assert notice["params"]["willRetry"] is True

        raw = upstream.next_request()
        request = ResponsesRequest.parse(raw)
        assert request.stream is True
        assert request.item_kinds == ["message:user"]
        assert request.messages("user")[-1].text == "Reply with exactly: CONNECTION_RETRY_OK"
        upstream.respond(raw, sse.response_stream([sse.Message("CONNECTION_RETRY_OK")], model=MODEL))

        assert scenarios.await_turn_completed(process)["params"]["turn"]["status"] == "completed"
        assert process.alive()
    captured = process.stdout_frames()
    frames.assert_success(captured, "CONNECTION_RETRY_OK")
    assert [error.will_retry for error in frames.errors(captured)] == [True]
    assert len(frames.agent_texts(captured)) == 1
    upstream.assert_quiescent()


def test_stream_lost_after_visible_text_is_retried_and_the_thread_continues(
    codex: CodexHarness, upstream: ScriptedUpstream
) -> None:
    with codex.start(upstream) as process:
        thread_id = scenarios.launch_handshake(process, cwd=str(codex.workspace), model=MODEL, effort=EFFORT)[
            "thread_id"
        ]
        scenarios.start_turn(
            process, thread_id=thread_id, request_id="capture-3", text="Reply with exactly: POST_FAILURE_FIRST_OK"
        )

        raw = upstream.next_request()
        upstream.respond(
            raw,
            sse.response_stream([sse.Message("POST_FAILURE_FIRST_OK")], model=MODEL).until(
                "response.output_text.delta"
            ),
        )
        notice = process.await_frame(lambda item: item.get("method") == "error", timeout=60)
        assert notice["params"]["willRetry"] is True

        raw = upstream.next_request()
        request = ResponsesRequest.parse(raw)
        # The retry resends the turn without the partial text.
        assert request.item_kinds == ["message:user"]
        upstream.respond(raw, sse.response_stream([sse.Message("POST_FAILURE_FIRST_OK")], model=MODEL))
        assert scenarios.await_turn_completed(process)["params"]["turn"]["status"] == "completed"

        scenarios.start_turn(
            process, thread_id=thread_id, request_id="capture-4", text="Reply with exactly: POST_FAILURE_FOLLOW_UP_OK"
        )
        raw = upstream.next_request()
        request = ResponsesRequest.parse(raw)
        assert request.item_kinds == ["message:user", "message:assistant", "message:user"]
        assert request.messages("assistant")[0].text == "POST_FAILURE_FIRST_OK"
        upstream.respond(raw, sse.response_stream([sse.Message("POST_FAILURE_FOLLOW_UP_OK")], model=MODEL))
        assert scenarios.await_turn_completed(process)["params"]["turn"]["status"] == "completed"
    captured = process.stdout_frames()
    frames.assert_success(captured, "POST_FAILURE_FOLLOW_UP_OK")
    assert [turn.status for turn in frames.completed_turns(captured)] == [wire.TurnStatus.COMPLETED] * 2
    upstream.assert_quiescent()


def test_retry_exhaustion_fails_the_turn_and_the_thread_accepts_the_next_input(
    codex: CodexHarness, upstream: ScriptedUpstream
) -> None:
    upstream.always(lambda _request: Refuse())
    with codex.start(upstream) as process:
        thread_id = scenarios.launch_handshake(process, cwd=str(codex.workspace), model=MODEL, effort=EFFORT)[
            "thread_id"
        ]
        scenarios.start_turn(
            process, thread_id=thread_id, request_id="capture-3", text="Reply with exactly: CONNECTION_EXHAUSTION_OK"
        )
        failed = scenarios.await_turn_completed(process, timeout_s=300)
        assert failed["params"]["turn"]["status"] == "failed"
        assert process.alive()
        assert len(upstream.observed) == 1 + scenarios.MAX_RETRIES
        upstream.clear_rules()

        scenarios.start_turn(
            process,
            thread_id=thread_id,
            request_id="capture-4",
            text="Reply with exactly: POST_EXHAUSTION_FOLLOW_UP_OK",
        )
        raw = upstream.next_request()
        request = ResponsesRequest.parse(raw)
        assert request.item_kinds == ["message:user", "message:user"]
        assert request.messages("user")[-1].text == "Reply with exactly: POST_EXHAUSTION_FOLLOW_UP_OK"
        upstream.respond(raw, sse.response_stream([sse.Message("POST_EXHAUSTION_FOLLOW_UP_OK")], model=MODEL))
        assert scenarios.await_turn_completed(process)["params"]["turn"]["status"] == "completed"
    captured = process.stdout_frames()
    assert [error.will_retry for error in frames.errors(captured)] == [True] * scenarios.MAX_RETRIES + [False]
    assert [turn.status for turn in frames.completed_turns(captured)] == [
        wire.TurnStatus.FAILED,
        wire.TurnStatus.COMPLETED,
    ]
    frames.assert_success(captured, "POST_EXHAUSTION_FOLLOW_UP_OK")
    upstream.assert_quiescent()


if __name__ == "__main__":
    pytest_bazel.main()
