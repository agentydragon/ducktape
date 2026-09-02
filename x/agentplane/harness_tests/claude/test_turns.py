"""Plain turns: one scripted exchange, and native session resume across processes."""

from __future__ import annotations

import pytest_bazel

from x.agentplane.harness_tests.claude import anthropic_sse as sse, frames
from x.agentplane.harness_tests.claude.harness import MODEL, ClaudeHarness
from x.agentplane.harness_tests.claude.requests import MessagesRequest
from x.agentplane.harness_tests.scripted_upstream import ScriptedUpstream
from x.agentplane.native.claude import scenarios


def test_baseline_turn(claude: ClaudeHarness, upstream: ScriptedUpstream) -> None:
    with claude.start(upstream) as process:
        scenarios.launch_handshake(process)
        scenarios.send(process, "Reply with exactly: CAPTURE_BASELINE_OK")

        raw = upstream.next_request()
        request = MessagesRequest.parse(raw)
        assert raw.path == "/v1/messages?beta=true"
        assert request.model == MODEL
        assert request.stream is True
        assert request.thinking.type == "enabled"
        assert request.tool_names == list(scenarios.TOOLS)
        assert request.system_text.endswith(scenarios.SYSTEM_PROMPT)
        assert len(request.system_text) < 1000
        assert request.texts("user")[-1] == "Reply with exactly: CAPTURE_BASELINE_OK"
        upstream.respond(
            raw, sse.message_stream([sse.Thinking("brief", "sig_test_1"), sse.Text("CAPTURE_BASELINE_OK")], model=MODEL)
        )

        result = scenarios.await_result(process)
        assert result["result"] == "CAPTURE_BASELINE_OK"
        assert process.alive()
    frames.assert_success(process.stdout_frames(), "CAPTURE_BASELINE_OK")
    assert len(upstream.observed) == 1
    upstream.assert_quiescent()


def test_idle_resume_replays_the_transcript_from_disk(claude: ClaudeHarness, upstream: ScriptedUpstream) -> None:
    with claude.start(upstream) as first:
        scenarios.launch_handshake(first)
        scenarios.send(first, "Reply with exactly: IDLE_RESUME_SEED_OK")
        raw = upstream.next_request()
        upstream.respond(raw, sse.message_stream([sse.Text("IDLE_RESUME_SEED_OK")], model=MODEL))
        seed = scenarios.await_result(first)
        assert seed["result"] == "IDLE_RESUME_SEED_OK"

    with claude.start(upstream, resume_id=scenarios.session_id(seed)) as second:
        scenarios.launch_handshake(second)
        scenarios.send(second, "Reply with exactly: IDLE_RESUME_OK")
        raw = upstream.next_request()
        request = MessagesRequest.parse(raw)
        assert request.texts("user")[-1] == "Reply with exactly: IDLE_RESUME_OK"
        assert "Reply with exactly: IDLE_RESUME_SEED_OK" in request.texts("user")
        assert request.texts("assistant") == ["IDLE_RESUME_SEED_OK"]
        upstream.respond(raw, sse.message_stream([sse.Text("IDLE_RESUME_OK")], model=MODEL))
        assert scenarios.await_result(second)["result"] == "IDLE_RESUME_OK"
    frames.assert_success(second.stdout_frames(), "IDLE_RESUME_OK")
    upstream.assert_quiescent()


if __name__ == "__main__":
    pytest_bazel.main()
