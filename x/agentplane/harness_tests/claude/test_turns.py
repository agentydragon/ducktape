"""Plain turns: one scripted exchange, and native session resume across processes."""

from __future__ import annotations

import pytest_bazel

from x.agentplane.harness_tests.claude import anthropic_sse as sse, frames
from x.agentplane.harness_tests.claude.harness import MODEL, ClaudeHarness
from x.agentplane.harness_tests.claude.requests import MessagesRequest
from x.agentplane.harness_tests.scripted_upstream import ScriptedUpstream
from x.agentplane.native.claude import driver, scenarios

CRASHED_SESSION = "00000000-0000-4000-8000-000000000001"
SEED_INPUT = "Reply with exactly: CRASH_RESUME_SEED_OK"
IN_FLIGHT_INPUT = "Reply with exactly: CRASHED_IN_FLIGHT_FATE"
QUEUED_INPUT = "Reply with exactly: CRASHED_QUEUE_FATE"
RECOVERY_INPUT = "Reply with exactly: CRASH_RESUME_OK"


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


def test_crash_before_a_completed_turn_leaves_claudes_session_unresumable(
    claude: ClaudeHarness, upstream: ScriptedUpstream
) -> None:
    """An in-flight request alone has no durable Claude conversation to resume."""
    with claude.start(upstream, session_id=CRASHED_SESSION) as first:
        scenarios.launch_handshake(first)
        scenarios.send(first, IN_FLIGHT_INPUT)
        raw = upstream.next_request()
        assert first.crash() < 0
    assert raw.client_closed.wait(30)

    with claude.start(upstream, resume_id=CRASHED_SESSION) as resumed:
        resumed.write(driver.initialize())
        failure = scenarios.await_result(resumed)
        assert failure["is_error"] is True
        assert failure["errors"] == [f"No conversation found with session ID: {CRASHED_SESSION}"]
    upstream.assert_quiescent()


def test_resume_after_crash_replays_completed_history_but_drops_active_and_queued_input(
    claude: ClaudeHarness, upstream: ScriptedUpstream
) -> None:
    """A killed resumed process retains prior durable history, not its active turn or queue."""
    with claude.start(upstream, session_id=CRASHED_SESSION) as seeded:
        scenarios.launch_handshake(seeded)
        scenarios.send(seeded, SEED_INPUT)
        seed_raw = upstream.next_request()
        upstream.respond(seed_raw, sse.message_stream([sse.Text("CRASH_RESUME_SEED_OK")], model=MODEL))
        seed = scenarios.await_result(seeded)
        assert seed["result"] == "CRASH_RESUME_SEED_OK"
        assert scenarios.session_id(seed) == CRASHED_SESSION

    with claude.start(upstream, resume_id=CRASHED_SESSION, replay_user_messages=True) as first:
        scenarios.launch_handshake(first)
        scenarios.send(first, IN_FLIGHT_INPUT)
        raw = upstream.next_request()

        queued = driver.user_frame(QUEUED_INPUT)
        first.write(queued)
        first.await_frame(
            lambda frame: (
                frame.get("type") == "command_lifecycle"
                and frame.get("command_uuid") == queued.uuid
                and frame.get("state") == "queued"
            ),
            timeout=30,
        )
        assert first.crash() < 0
    assert raw.client_closed.wait(30)

    with claude.start(upstream, resume_id=CRASHED_SESSION) as resumed:
        scenarios.launch_handshake(resumed)
        scenarios.send(resumed, RECOVERY_INPUT)
        recovery_raw = upstream.next_request()
        replay = MessagesRequest.parse(recovery_raw)
        user_texts = replay.texts("user")
        assert user_texts[-1] == RECOVERY_INPUT
        assert SEED_INPUT in user_texts
        assert QUEUED_INPUT not in user_texts
        assert IN_FLIGHT_INPUT not in user_texts
        assert replay.texts("assistant") == ["CRASH_RESUME_SEED_OK"]
        upstream.respond(recovery_raw, sse.message_stream([sse.Text("CRASH_RESUME_OK")], model=MODEL))
        assert scenarios.await_result(resumed)["result"] == "CRASH_RESUME_OK"
    upstream.assert_quiescent()


if __name__ == "__main__":
    pytest_bazel.main()
