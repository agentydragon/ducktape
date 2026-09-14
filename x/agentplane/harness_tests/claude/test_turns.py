"""Plain turns: one scripted exchange, and native session resume across processes."""

from __future__ import annotations

import pytest_bazel

from x.agentplane.harness_tests.claude import anthropic_sse as sse, frames
from x.agentplane.harness_tests.claude.harness import MODEL, ClaudeHarness
from x.agentplane.harness_tests.claude.messages import AnthropicMessages
from x.agentplane.native.claude import wire
from x.agentplane.native.claude.blocks import TextBlock, blocks_of
from x.agentplane.native.claude.scenarios import SYSTEM_PROMPT, TOOLS

CRASHED_SESSION = "00000000-0000-4000-8000-000000000001"
SEED_INPUT = "Reply with exactly: CRASH_RESUME_SEED_OK"
IN_FLIGHT_INPUT = "Reply with exactly: CRASHED_IN_FLIGHT_FATE"
QUEUED_INPUT = "Reply with exactly: CRASHED_QUEUE_FATE"
RECOVERY_INPUT = "Reply with exactly: CRASH_RESUME_OK"
INTERRUPTED_RESUME_INPUT = "Reply with exactly: INTERRUPTED_RESUME_INPUT"
INTERRUPTED_RESUME_PARTIAL = "INTERRUPTED_RESUME_PARTIAL"
INTERRUPTED_RESUME_RECOVERY = "Reply with exactly: INTERRUPTED_RESUME_RECOVERY_OK"


async def test_baseline_turn(claude: ClaudeHarness, anthropic_messages: AnthropicMessages) -> None:
    async with claude.start(anthropic_messages) as run:
        prompt = await run.send("Reply with exactly: CAPTURE_BASELINE_OK")

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

        assert (await prompt.result()).result == "CAPTURE_BASELINE_OK"
        assert run.running
    frames.assert_success(run.native_frames(), "CAPTURE_BASELINE_OK")


async def test_idle_resume_replays_the_transcript_from_disk(
    claude: ClaudeHarness, anthropic_messages: AnthropicMessages
) -> None:
    async with claude.start(anthropic_messages) as first:
        prompt = await first.send("Reply with exactly: IDLE_RESUME_SEED_OK")
        async with await anthropic_messages.await_next_request() as exchange:
            stream = sse.message_stream([sse.Text("IDLE_RESUME_SEED_OK")], model=MODEL)
            await exchange.send(*stream.events)
        seed = await prompt.result()
        assert seed.result == "IDLE_RESUME_SEED_OK"

    async with claude.start(anthropic_messages, resume_id=seed.session_id) as second:
        prompt = await second.send("Reply with exactly: IDLE_RESUME_OK")
        async with await anthropic_messages.await_next_request() as exchange:
            request = exchange.request
            assert request.texts("user")[-1] == "Reply with exactly: IDLE_RESUME_OK"
            assert "Reply with exactly: IDLE_RESUME_SEED_OK" in request.texts("user")
            assert request.texts("assistant") == ["IDLE_RESUME_SEED_OK"]
            stream = sse.message_stream([sse.Text("IDLE_RESUME_OK")], model=MODEL)
            await exchange.send(*stream.events)
        assert (await prompt.result()).result == "IDLE_RESUME_OK"
    frames.assert_success(second.native_frames(), "IDLE_RESUME_OK")


async def test_resume_after_an_interrupted_partial_turn_replays_only_completed_model_history(
    claude: ClaudeHarness, anthropic_messages: AnthropicMessages
) -> None:
    """Claude emits a partial turn and marker but does not replay them to a fresh model request.

    This is deliberately distinct from a process killed while its turn is active below: the first
    harness observes the interrupt and closes its upstream stream before it is killed.  The resumed
    request is the native boundary the runner can rely on, so assert its typed Anthropic context
    rather than inferring durability from the stream-json terminal frame.
    """
    async with claude.start(anthropic_messages, session_id=CRASHED_SESSION) as seeded:
        seed_prompt = await seeded.send(SEED_INPUT)
        async with await anthropic_messages.await_next_request() as exchange:
            await exchange.send(*sse.message_stream([sse.Text("CRASH_RESUME_SEED_OK")], model=MODEL).events)
        seed = await seed_prompt.result()
        assert seed.session_id == CRASHED_SESSION

    async with claude.start(anthropic_messages, resume_id=CRASHED_SESSION) as interrupted:
        prompt = await interrupted.send(INTERRUPTED_RESUME_INPUT)
        async with await anthropic_messages.await_next_request() as exchange:
            await exchange.send(
                *sse.message_stream([sse.Text(INTERRUPTED_RESUME_PARTIAL)], model=MODEL).through("text_delta").events
            )
            await prompt.active()
            assert (await interrupted.interrupt(cancel_queued=False)).response.subtype == "success"
            await exchange.wait_client_closed()
        assert (await prompt.result()).is_error is True
        interrupted_frames = frames.parse(interrupted.native_frames())
        assert INTERRUPTED_RESUME_PARTIAL in frames.assistant_texts(interrupted.native_frames())
        assert "[Request interrupted by user]" in [
            block.text
            for frame in interrupted_frames
            if isinstance(frame, wire.UserFrame)
            for block in blocks_of(frame.message.content)
            if isinstance(block, TextBlock)
        ]
        assert await interrupted.crash() < 0

    async with claude.start(anthropic_messages, resume_id=CRASHED_SESSION) as resumed:
        recovery = await resumed.send(INTERRUPTED_RESUME_RECOVERY)
        async with await anthropic_messages.await_next_request() as exchange:
            replay = exchange.request
            conversation_user_texts = [
                text for text in replay.texts("user") if not text.startswith("<system-reminder>")
            ]
            assert conversation_user_texts == [SEED_INPUT, INTERRUPTED_RESUME_RECOVERY]
            assert replay.texts("assistant") == ["CRASH_RESUME_SEED_OK"]
            await exchange.send(*sse.message_stream([sse.Text("INTERRUPTED_RESUME_RECOVERY_OK")], model=MODEL).events)
        assert (await recovery.result()).result == "INTERRUPTED_RESUME_RECOVERY_OK"


async def test_crash_before_a_completed_turn_leaves_claudes_session_unresumable(
    claude: ClaudeHarness, anthropic_messages: AnthropicMessages
) -> None:
    """An in-flight request alone has no durable Claude conversation to resume."""
    async with claude.start(anthropic_messages, session_id=CRASHED_SESSION) as first:
        await first.send(IN_FLIGHT_INPUT)
        async with await anthropic_messages.await_next_request() as exchange:
            assert await first.crash() < 0
            await exchange.wait_client_closed()

    async with claude.start(anthropic_messages, resume_id=CRASHED_SESSION, initialize=False) as resumed:
        failure = await resumed.initialize()
        assert isinstance(failure, wire.ResultFrame)
        assert failure.is_error is True
        assert failure.errors == [f"No conversation found with session ID: {CRASHED_SESSION}"]


async def test_resume_after_crash_replays_completed_history_but_drops_active_and_queued_input(
    claude: ClaudeHarness, anthropic_messages: AnthropicMessages
) -> None:
    """A killed resumed process retains prior durable history, not its active turn or queue."""
    async with claude.start(anthropic_messages, session_id=CRASHED_SESSION) as seeded:
        prompt = await seeded.send(SEED_INPUT)
        async with await anthropic_messages.await_next_request() as exchange:
            stream = sse.message_stream([sse.Text("CRASH_RESUME_SEED_OK")], model=MODEL)
            await exchange.send(*stream.events)
        seed = await prompt.result()
        assert seed.result == "CRASH_RESUME_SEED_OK"
        assert seed.session_id == CRASHED_SESSION

    async with claude.start(anthropic_messages, resume_id=CRASHED_SESSION, replay_user_messages=True) as first:
        await first.send(IN_FLIGHT_INPUT)
        async with await anthropic_messages.await_next_request() as exchange:
            (queued,) = await first.send_many([QUEUED_INPUT])
            await queued.lifecycle(wire.CommandState.QUEUED)
            assert await first.crash() < 0
            await exchange.wait_client_closed()

    async with claude.start(anthropic_messages, resume_id=CRASHED_SESSION) as resumed:
        prompt = await resumed.send(RECOVERY_INPUT)
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
        assert (await prompt.result()).result == "CRASH_RESUME_OK"


if __name__ == "__main__":
    pytest_bazel.main()
