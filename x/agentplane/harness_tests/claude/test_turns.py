"""Plain turns: one scripted exchange, and native session resume across processes."""

from __future__ import annotations

import pytest_bazel

from x.agentplane.harness_tests.claude import anthropic_sse as sse, frames
from x.agentplane.harness_tests.claude.harness import MODEL, ClaudeHarness
from x.agentplane.harness_tests.claude.messages import AnthropicMessages
from x.agentplane.native.claude import async_scenarios as scenarios, driver
from x.agentplane.native.claude.scenarios import SYSTEM_PROMPT, TOOLS, session_id

CRASHED_SESSION = "00000000-0000-4000-8000-000000000001"
SEED_INPUT = "Reply with exactly: CRASH_RESUME_SEED_OK"
IN_FLIGHT_INPUT = "Reply with exactly: CRASHED_IN_FLIGHT_FATE"
QUEUED_INPUT = "Reply with exactly: CRASHED_QUEUE_FATE"
RECOVERY_INPUT = "Reply with exactly: CRASH_RESUME_OK"


async def test_baseline_turn(claude: ClaudeHarness, anthropic_messages: AnthropicMessages) -> None:
    async with claude.start(anthropic_messages) as process:
        await scenarios.launch_handshake(process)
        await scenarios.send(process, "Reply with exactly: CAPTURE_BASELINE_OK")

        async with await anthropic_messages.await_next_request() as exchange:
            request = exchange.request
            assert request.model == MODEL
            assert request.stream is True
            assert request.thinking.type == "enabled"
            assert request.tool_names == list(TOOLS)
            assert request.system_text.endswith(SYSTEM_PROMPT)
            assert len(request.system_text) < 1000
            assert request.texts("user")[-1] == "Reply with exactly: CAPTURE_BASELINE_OK"
            stream = sse.message_stream(
                [sse.Thinking("brief", "sig_test_1"), sse.Text("CAPTURE_BASELINE_OK")], model=MODEL
            )
            await exchange.send(*stream.events)

        result = await scenarios.await_result(process)
        assert result["result"] == "CAPTURE_BASELINE_OK"
        assert process.alive()
    frames.assert_success(process.stdout_frames(), "CAPTURE_BASELINE_OK")


async def test_idle_resume_replays_the_transcript_from_disk(
    claude: ClaudeHarness, anthropic_messages: AnthropicMessages
) -> None:
    async with claude.start(anthropic_messages) as first:
        await scenarios.launch_handshake(first)
        await scenarios.send(first, "Reply with exactly: IDLE_RESUME_SEED_OK")
        async with await anthropic_messages.await_next_request() as exchange:
            stream = sse.message_stream([sse.Text("IDLE_RESUME_SEED_OK")], model=MODEL)
            await exchange.send(*stream.events)
        seed = await scenarios.await_result(first)
        assert seed["result"] == "IDLE_RESUME_SEED_OK"

    async with claude.start(anthropic_messages, resume_id=session_id(seed)) as second:
        await scenarios.launch_handshake(second)
        await scenarios.send(second, "Reply with exactly: IDLE_RESUME_OK")
        async with await anthropic_messages.await_next_request() as exchange:
            request = exchange.request
            assert request.texts("user")[-1] == "Reply with exactly: IDLE_RESUME_OK"
            assert "Reply with exactly: IDLE_RESUME_SEED_OK" in request.texts("user")
            assert request.texts("assistant") == ["IDLE_RESUME_SEED_OK"]
            stream = sse.message_stream([sse.Text("IDLE_RESUME_OK")], model=MODEL)
            await exchange.send(*stream.events)
        assert (await scenarios.await_result(second))["result"] == "IDLE_RESUME_OK"
    frames.assert_success(second.stdout_frames(), "IDLE_RESUME_OK")


async def test_crash_before_a_completed_turn_leaves_claudes_session_unresumable(
    claude: ClaudeHarness, anthropic_messages: AnthropicMessages
) -> None:
    """An in-flight request alone has no durable Claude conversation to resume."""
    async with claude.start(anthropic_messages, session_id=CRASHED_SESSION) as first:
        await scenarios.launch_handshake(first)
        await scenarios.send(first, IN_FLIGHT_INPUT)
        async with await anthropic_messages.await_next_request() as exchange:
            assert await first.crash() < 0
            await exchange.wait_client_closed()

    async with claude.start(anthropic_messages, resume_id=CRASHED_SESSION) as resumed:
        await resumed.send(driver.initialize())
        failure = await scenarios.await_result(resumed)
        assert failure["is_error"] is True
        assert failure["errors"] == [f"No conversation found with session ID: {CRASHED_SESSION}"]


async def test_resume_after_crash_replays_completed_history_but_drops_active_and_queued_input(
    claude: ClaudeHarness, anthropic_messages: AnthropicMessages
) -> None:
    """A killed resumed process retains prior durable history, not its active turn or queue."""
    async with claude.start(anthropic_messages, session_id=CRASHED_SESSION) as seeded:
        await scenarios.launch_handshake(seeded)
        await scenarios.send(seeded, SEED_INPUT)
        async with await anthropic_messages.await_next_request() as exchange:
            stream = sse.message_stream([sse.Text("CRASH_RESUME_SEED_OK")], model=MODEL)
            await exchange.send(*stream.events)
        seed = await scenarios.await_result(seeded)
        assert seed["result"] == "CRASH_RESUME_SEED_OK"
        assert session_id(seed) == CRASHED_SESSION

    async with claude.start(anthropic_messages, resume_id=CRASHED_SESSION, replay_user_messages=True) as first:
        await scenarios.launch_handshake(first)
        await scenarios.send(first, IN_FLIGHT_INPUT)
        async with await anthropic_messages.await_next_request() as exchange:
            queued = driver.user_frame(QUEUED_INPUT)
            await first.send(queued)
            while True:
                frame = await first.next_frame()
                if (
                    frame.get("type") == "command_lifecycle"
                    and frame.get("command_uuid") == queued.uuid
                    and frame.get("state") == "queued"
                ):
                    break
            assert await first.crash() < 0
            await exchange.wait_client_closed()

    async with claude.start(anthropic_messages, resume_id=CRASHED_SESSION) as resumed:
        await scenarios.launch_handshake(resumed)
        await scenarios.send(resumed, RECOVERY_INPUT)
        async with await anthropic_messages.await_next_request() as exchange:
            replay = exchange.request
            user_texts = replay.texts("user")
            assert user_texts[-1] == RECOVERY_INPUT
            assert SEED_INPUT in user_texts
            assert QUEUED_INPUT not in user_texts
            assert IN_FLIGHT_INPUT not in user_texts
            assert replay.texts("assistant") == ["CRASH_RESUME_SEED_OK"]
            stream = sse.message_stream([sse.Text("CRASH_RESUME_OK")], model=MODEL)
            await exchange.send(*stream.events)
        assert (await scenarios.await_result(resumed))["result"] == "CRASH_RESUME_OK"


if __name__ == "__main__":
    pytest_bazel.main()
