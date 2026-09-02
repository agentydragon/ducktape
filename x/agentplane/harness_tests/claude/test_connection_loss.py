"""Upstream connection loss: native retry, the non-streaming fallback, and retry exhaustion."""

from __future__ import annotations

import pytest_bazel

from x.agentplane.harness_tests.claude import anthropic_sse as sse, frames
from x.agentplane.harness_tests.claude.harness import MODEL, ClaudeHarness
from x.agentplane.harness_tests.claude.requests import MessagesRequest
from x.agentplane.harness_tests.scripted_upstream import Refuse, ScriptedUpstream
from x.agentplane.native.claude import scenarios


def test_stream_lost_before_content_is_retried_without_streaming(
    claude: ClaudeHarness, upstream: ScriptedUpstream
) -> None:
    with claude.start(upstream) as process:
        scenarios.launch_handshake(process)
        scenarios.send(process, "Reply with exactly: CONNECTION_RETRY_OK")

        raw = upstream.next_request()
        assert MessagesRequest.parse(raw).stream is True
        upstream.respond(raw, sse.message_stream([sse.Text("lost")], model=MODEL).until("message_start"))

        # Claude Code retries the same turn as a non-streaming request.
        raw = upstream.next_request()
        request = MessagesRequest.parse(raw)
        assert request.stream is False
        assert request.texts("user")[-1] == "Reply with exactly: CONNECTION_RETRY_OK"
        assert request.texts("assistant") == []
        upstream.respond(raw, sse.message_body([sse.Text("CONNECTION_RETRY_OK")], model=MODEL))

        assert scenarios.await_result(process)["result"] == "CONNECTION_RETRY_OK"
        assert process.alive()
    captured = process.stdout_frames()
    frames.assert_success(captured, "CONNECTION_RETRY_OK")
    assert len(frames.assistant_texts(captured)) == 1
    upstream.assert_quiescent()


def test_stream_lost_after_visible_text_is_retried_without_streaming(
    claude: ClaudeHarness, upstream: ScriptedUpstream
) -> None:
    with claude.start(upstream) as process:
        scenarios.launch_handshake(process)
        scenarios.send(process, "Reply with exactly: POST_FAILURE_FIRST_OK")

        raw = upstream.next_request()
        upstream.respond(raw, sse.message_stream([sse.Text("POST_FAILURE_FIRST_OK")], model=MODEL).until("text_delta"))

        # The visible partial text is discarded and the whole turn is retried without streaming.
        raw = upstream.next_request()
        request = MessagesRequest.parse(raw)
        assert request.stream is False
        assert request.texts("assistant") == []
        upstream.respond(raw, sse.message_body([sse.Text("POST_FAILURE_FIRST_OK")], model=MODEL))
        assert scenarios.await_result(process)["result"] == "POST_FAILURE_FIRST_OK"
        assert process.alive()

        scenarios.send(process, "Reply with exactly: POST_FAILURE_FOLLOW_UP_OK")
        raw = upstream.next_request()
        request = MessagesRequest.parse(raw)
        assert request.texts("user")[-1] == "Reply with exactly: POST_FAILURE_FOLLOW_UP_OK"
        assert request.texts("assistant") == ["POST_FAILURE_FIRST_OK"]
        upstream.respond(raw, sse.message_stream([sse.Text("POST_FAILURE_FOLLOW_UP_OK")], model=MODEL))
        assert scenarios.await_result(process)["result"] == "POST_FAILURE_FOLLOW_UP_OK"
    captured = process.stdout_frames()
    assert [terminal.is_error for terminal in frames.terminals(captured)] == [False, False]
    assert frames.assistant_texts(captured) == ["POST_FAILURE_FIRST_OK", "POST_FAILURE_FOLLOW_UP_OK"]
    upstream.assert_quiescent()


def test_retry_exhaustion_fails_the_turn_and_the_process_accepts_the_next_input(
    claude: ClaudeHarness, upstream: ScriptedUpstream
) -> None:
    upstream.always(lambda _request: Refuse())
    with claude.start(upstream) as process:
        scenarios.launch_handshake(process)
        scenarios.send(process, "Reply with exactly: CONNECTION_EXHAUSTION_OK")
        failed = scenarios.await_result(process, timeout_s=300)
        assert failed["is_error"] is True
        assert process.alive()
        assert len(upstream.observed) == 1 + scenarios.MAX_RETRIES
        upstream.clear_rules()

        scenarios.send(process, "Reply with exactly: POST_EXHAUSTION_FOLLOW_UP_OK")
        raw = upstream.next_request()
        request = MessagesRequest.parse(raw)
        assert request.texts("user")[-1] == "Reply with exactly: POST_EXHAUSTION_FOLLOW_UP_OK"
        assert request.texts("assistant") == []
        upstream.respond(raw, sse.message_stream([sse.Text("POST_EXHAUSTION_FOLLOW_UP_OK")], model=MODEL))
        assert scenarios.await_result(process)["result"] == "POST_EXHAUSTION_FOLLOW_UP_OK"
    captured = process.stdout_frames()
    frames.assert_failure(frames.terminals(captured)[0], result_fragment="API Error", terminal_reason="api_error")
    assert len(frames.retry_notices(captured)) == scenarios.MAX_RETRIES
    assert [terminal.is_error for terminal in frames.terminals(captured)] == [True, False]
    frames.assert_success(captured, "POST_EXHAUSTION_FOLLOW_UP_OK")
    upstream.assert_quiescent()


if __name__ == "__main__":
    pytest_bazel.main()
