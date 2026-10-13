"""Plain turns: one scripted exchange, and native session resume across processes."""

from __future__ import annotations

import json
from collections.abc import Callable

import pytest
import pytest_bazel
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_delay, wait_fixed

from agentplane.harness_tests.claude import anthropic_sse, frames
from agentplane.harness_tests.claude.harness import MODEL, ClaudeLauncher
from agentplane.harness_tests.claude.messages import AnthropicMessages
from agentplane.harness_tests.claude.requests import MessagesRequest
from agentplane.native.claude import wire
from agentplane.native.claude.blocks import TextBlock, ThinkingBlock, blocks_of
from agentplane.native.claude.scenarios import SYSTEM_PROMPT, TOOLS
from agentplane.native.claude.testing import async_run
from agentplane.native.testing.async_process import NativeProcessEofError

CRASHED_SESSION = "00000000-0000-4000-8000-000000000001"
SEED_INPUT = "Reply with exactly: CRASH_RESUME_SEED_OK"
IN_FLIGHT_INPUT = "Reply with exactly: CRASHED_IN_FLIGHT_FATE"
QUEUED_INPUT = "Reply with exactly: CRASHED_QUEUE_FATE"
RECOVERY_INPUT = "Reply with exactly: CRASH_RESUME_OK"
INTERRUPTED_RESUME_INPUT = "Reply with exactly: INTERRUPTED_RESUME_INPUT"
INTERRUPTED_RESUME_PARTIAL = "INTERRUPTED_RESUME_PARTIAL"
INTERRUPTED_RESUME_RECOVERY = "Reply with exactly: INTERRUPTED_RESUME_RECOVERY_OK"
RETAINED_QUEUE_ACTIVE = "Keep this turn active until the process is killed."
INTERRUPT_MARKER = "[Request interrupted by user]"
CONTINUE_MARKER = "Continue from where you left off."
NO_RESPONSE = "No response requested."
RETAINED_QUEUE_FIRST = "Reply only after seeing RETAINED_QUEUE_CRASH_FIRST."
RETAINED_QUEUE_SECOND = "Reply only after seeing RETAINED_QUEUE_CRASH_SECOND."
RETAINED_QUEUE_RECOVERY = "Reply with exactly: RETAINED_QUEUE_CRASH_RECOVERY_OK"
KILLED_TOOL_INPUT = "Run the shell command."
INTERRUPTED_THINKING_INPUT = "Think, then answer."
RESUMED_AFTER_INTERRUPT_INPUT = "Reply with exactly: RESUMED_OK"


async def _until(condition: Callable[[], bool]) -> None:
    async for attempt in AsyncRetrying(
        stop=stop_after_delay(10), wait=wait_fixed(0.05), retry=retry_if_exception_type(AssertionError), reraise=True
    ):
        with attempt:
            assert condition()


def _transcript(claude: ClaudeLauncher) -> str:
    (path,) = (claude.config / "projects").glob(f"*/{CRASHED_SESSION}.jsonl")
    return path.read_text()


async def _until_persisted(claude: ClaudeLauncher, text: str) -> None:
    """Claude writes the transcript lazily, so what a kill leaves on disk depends on its timing.

    A test about the resume after a kill waits for the entry it depends on rather than racing it.
    """
    await _until(lambda: text in _transcript(claude))


def _conversation_user_texts(request: MessagesRequest) -> list[str]:
    """The request's user texts minus the system reminders Claude Code injects among them."""
    return [text.strip() for text in request.texts("user") if not text.startswith("<system-reminder>")]


async def _thinking_completed(run: async_run.ClaudeRun) -> None:
    events = run.events()
    while True:
        frame = await events.next()
        if isinstance(frame, wire.AssistantFrame) and any(
            isinstance(block, ThinkingBlock) for block in frame.message.content
        ):
            return


async def _seed_crashed_session(claude: ClaudeLauncher, anthropic_messages: AnthropicMessages) -> wire.ResultFrame:
    """Complete one turn under CRASHED_SESSION for the crash/resume tests below to build on."""
    async with claude.start(anthropic_messages, session_id=CRASHED_SESSION) as seeded:
        seed_prompt = await seeded.send(SEED_INPUT)
        async with await anthropic_messages.await_next_request() as exchange:
            await exchange.send(
                *anthropic_sse.message_stream([anthropic_sse.Text("CRASH_RESUME_SEED_OK")], model=MODEL).events
            )
        seed = await seed_prompt.result()
    assert seed.result == "CRASH_RESUME_SEED_OK"
    assert seed.session_id == CRASHED_SESSION
    return seed


async def test_baseline_turn(claude: ClaudeLauncher, anthropic_messages: AnthropicMessages) -> None:
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
            stream = anthropic_sse.message_stream(
                [anthropic_sse.Thinking("brief", "sig_test_1"), anthropic_sse.Text("CAPTURE_BASELINE_OK")], model=MODEL
            )
            await exchange.send(*stream.events)

        assert (await prompt.result()).result == "CAPTURE_BASELINE_OK"
        assert run.running
    frames.assert_success(run.native_frames(), "CAPTURE_BASELINE_OK")


async def test_idle_resume_replays_the_transcript_from_disk(
    claude: ClaudeLauncher, anthropic_messages: AnthropicMessages
) -> None:
    async with claude.start(anthropic_messages) as first:
        prompt = await first.send("Reply with exactly: IDLE_RESUME_SEED_OK")
        async with await anthropic_messages.await_next_request() as exchange:
            stream = anthropic_sse.message_stream([anthropic_sse.Text("IDLE_RESUME_SEED_OK")], model=MODEL)
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
            stream = anthropic_sse.message_stream([anthropic_sse.Text("IDLE_RESUME_OK")], model=MODEL)
            await exchange.send(*stream.events)
        assert (await prompt.result()).result == "IDLE_RESUME_OK"
    frames.assert_success(second.native_frames(), "IDLE_RESUME_OK")


async def test_resume_after_an_interrupted_partial_turn_replays_the_partial_turn_and_marker(
    claude: ClaudeLauncher, anthropic_messages: AnthropicMessages
) -> None:
    """Once Claude has written an interrupted turn, a fresh model request carries it and its marker.

    This is deliberately distinct from a process killed while its turn is active below: the first
    harness observes the interrupt and closes its upstream stream before it is killed.  The resumed
    request is the native boundary the runner can rely on, so assert its typed Anthropic context
    rather than inferring durability from the stream-json terminal frame.
    """
    await _seed_crashed_session(claude, anthropic_messages)

    async with claude.start(anthropic_messages, resume_id=CRASHED_SESSION) as interrupted:
        prompt = await interrupted.send(INTERRUPTED_RESUME_INPUT)
        async with await anthropic_messages.await_next_request() as exchange:
            await exchange.send(
                *anthropic_sse.message_stream([anthropic_sse.Text(INTERRUPTED_RESUME_PARTIAL)], model=MODEL)
                .through("text_delta")
                .events
            )
            await prompt.active()
            assert (await interrupted.interrupt(cancel_queued=False)).response.subtype == "success"
            await exchange.wait_client_closed()
        assert (await prompt.result()).is_error is True
        interrupted_frames = frames.parse(interrupted.native_frames())
        assert INTERRUPTED_RESUME_PARTIAL in frames.assistant_texts(interrupted.native_frames())
        assert INTERRUPT_MARKER in [
            block.text
            for frame in interrupted_frames
            if isinstance(frame, wire.UserFrame)
            for block in blocks_of(frame.message.content)
            if isinstance(block, TextBlock)
        ]
        await _until_persisted(claude, INTERRUPT_MARKER)
        assert await interrupted.crash() < 0

    async with claude.start(anthropic_messages, resume_id=CRASHED_SESSION) as resumed:
        recovery = await resumed.send(INTERRUPTED_RESUME_RECOVERY)
        async with await anthropic_messages.await_next_request() as exchange:
            replay = exchange.request
            assert _conversation_user_texts(replay) == [
                SEED_INPUT,
                INTERRUPTED_RESUME_INPUT,
                INTERRUPT_MARKER,
                INTERRUPTED_RESUME_RECOVERY,
            ]
            assert replay.texts("assistant") == ["CRASH_RESUME_SEED_OK", INTERRUPTED_RESUME_PARTIAL, NO_RESPONSE]
            await exchange.send(
                *anthropic_sse.message_stream(
                    [anthropic_sse.Text("INTERRUPTED_RESUME_RECOVERY_OK")], model=MODEL
                ).events
            )
        assert (await recovery.result()).result == "INTERRUPTED_RESUME_RECOVERY_OK"


async def test_resume_after_interrupt_replays_the_queued_inputs_its_next_turn_took(
    claude: ClaudeLauncher, anthropic_messages: AnthropicMessages
) -> None:
    """Inputs an interrupt leaves queued run together as the next turn; a kill during that turn
    leaves them in the resumed history, as it leaves any other turn Claude has written."""
    await _seed_crashed_session(claude, anthropic_messages)

    async with claude.start(anthropic_messages, resume_id=CRASHED_SESSION, replay_user_messages=True) as interrupted:
        active = await interrupted.send(RETAINED_QUEUE_ACTIVE)
        async with await anthropic_messages.await_next_request() as exchange:
            await exchange.send(
                *anthropic_sse.message_stream([anthropic_sse.Text("never finished")], model=MODEL)
                .through("content_block_start")
                .events
            )
            await active.active()

            first, second = await interrupted.send_many([RETAINED_QUEUE_FIRST, RETAINED_QUEUE_SECOND])
            for queued in (first, second):
                await queued.lifecycle(wire.CommandState.QUEUED)

            receipt = await interrupted.interrupt(cancel_queued=False)
            assert receipt.response.subtype == "success"
            assert receipt.response.response == {"still_queued": [first.uuid, second.uuid]}
            await exchange.wait_client_closed()
        assert (await active.result()).is_error is True
        async with await anthropic_messages.await_next_request() as drained:
            assert _conversation_user_texts(drained.request)[-1] == f"{RETAINED_QUEUE_FIRST}\n{RETAINED_QUEUE_SECOND}"
            await _until_persisted(claude, RETAINED_QUEUE_SECOND)
            assert await interrupted.crash() < 0
            await drained.wait_client_closed()

    async with claude.start(anthropic_messages, resume_id=CRASHED_SESSION) as resumed:
        recovery = await resumed.send(RETAINED_QUEUE_RECOVERY)
        async with await anthropic_messages.await_next_request() as exchange:
            replay = exchange.request
            assert _conversation_user_texts(replay) == [
                SEED_INPUT,
                RETAINED_QUEUE_ACTIVE,
                INTERRUPT_MARKER,
                f"{RETAINED_QUEUE_FIRST}\n{RETAINED_QUEUE_SECOND}",
                CONTINUE_MARKER,
                RETAINED_QUEUE_RECOVERY,
            ]
            assert replay.texts("assistant") == ["CRASH_RESUME_SEED_OK", NO_RESPONSE]
            await exchange.send(
                *anthropic_sse.message_stream(
                    [anthropic_sse.Text("RETAINED_QUEUE_CRASH_RECOVERY_OK")], model=MODEL
                ).events
            )
        assert (await recovery.result()).result == "RETAINED_QUEUE_CRASH_RECOVERY_OK"


async def test_crash_before_a_completed_turn_leaves_claudes_session_unresumable(
    claude: ClaudeLauncher, anthropic_messages: AnthropicMessages
) -> None:
    """An in-flight request alone has no durable Claude conversation to resume."""
    async with claude.start(anthropic_messages, session_id=CRASHED_SESSION) as first:
        await first.send(IN_FLIGHT_INPUT)
        async with await anthropic_messages.await_next_request() as exchange:
            assert await first.crash() < 0
            await exchange.wait_client_closed()

    async with claude.start(anthropic_messages, resume_id=CRASHED_SESSION, initialize=False) as resumed:
        events = resumed.events()
        with pytest.raises(NativeProcessEofError, match="stdout closed"):
            await resumed.initialize()
        failure = await events.result()
        assert failure.is_error is True
        assert failure.errors == [f"No conversation found with session ID: {CRASHED_SESSION}"]


async def test_resume_after_crash_replays_the_active_input_but_not_the_queued_one(
    claude: ClaudeLauncher, anthropic_messages: AnthropicMessages
) -> None:
    """A killed process leaves its active turn's input in the history but not the input queued behind it."""
    await _seed_crashed_session(claude, anthropic_messages)

    async with claude.start(anthropic_messages, resume_id=CRASHED_SESSION, replay_user_messages=True) as first:
        await first.send(IN_FLIGHT_INPUT)
        async with await anthropic_messages.await_next_request() as exchange:
            (queued,) = await first.send_many([QUEUED_INPUT])
            await queued.lifecycle(wire.CommandState.QUEUED)
            await _until_persisted(claude, IN_FLIGHT_INPUT)
            assert await first.crash() < 0
            await exchange.wait_client_closed()

    async with claude.start(anthropic_messages, resume_id=CRASHED_SESSION) as resumed:
        prompt = await resumed.send(RECOVERY_INPUT)
        async with await anthropic_messages.await_next_request() as exchange:
            replay = exchange.request
            assert _conversation_user_texts(replay) == [SEED_INPUT, IN_FLIGHT_INPUT, CONTINUE_MARKER, RECOVERY_INPUT]
            assert replay.texts("assistant") == ["CRASH_RESUME_SEED_OK", NO_RESPONSE]
            stream = anthropic_sse.message_stream([anthropic_sse.Text("CRASH_RESUME_OK")], model=MODEL)
            await exchange.send(*stream.events)
        assert (await prompt.result()).result == "CRASH_RESUME_OK"


async def test_thinking_ahead_of_a_tool_killed_mid_run_is_saved_but_not_replayed(
    claude: ClaudeLauncher, anthropic_messages: AnthropicMessages
) -> None:
    """The transcript holds the thinking block as an entry of its own under the id of its message,
    yet the resume loads neither it nor the tool call: a message left holding only thinking is
    dropped. `runner/claude_history.py` mirrors this."""
    await _seed_crashed_session(claude, anthropic_messages)

    async with claude.start(anthropic_messages, resume_id=CRASHED_SESSION) as first:
        await first.send(KILLED_TOOL_INPUT)
        async with await anthropic_messages.await_next_request() as exchange:
            await exchange.send(
                *anthropic_sse.message_stream(
                    [
                        anthropic_sse.Thinking("KILLED_TOOL_THOUGHT", "sig_killed_tool"),
                        anthropic_sse.ToolUse("toolu_killed", "Bash", {"command": "touch tool_started; sleep 60"}),
                    ],
                    model=MODEL,
                ).events
            )
        await _until(lambda: (claude.workspace / "tool_started").exists() and "toolu_killed" in _transcript(claude))
        assert await first.crash() < 0

    saved = [json.loads(line) for line in _transcript(claude).splitlines()]
    thinking, tool_use = [entry["message"] for entry in saved if entry["type"] == "assistant"][-2:]
    assert thinking["id"] == tool_use["id"]
    assert [thinking["content"][0]["thinking"], tool_use["content"][0]["id"]] == ["KILLED_TOOL_THOUGHT", "toolu_killed"]

    async with claude.start(anthropic_messages, resume_id=CRASHED_SESSION) as resumed:
        recovery = await resumed.send(RECOVERY_INPUT)
        async with await anthropic_messages.await_next_request() as exchange:
            assert exchange.request.thinking_blocks == []
            assert exchange.request.tool_uses == []
            await exchange.send(
                *anthropic_sse.message_stream([anthropic_sse.Text("CRASH_RESUME_OK")], model=MODEL).events
            )
        assert (await recovery.result()).result == "CRASH_RESUME_OK"


async def test_thinking_interrupted_before_any_answer_is_not_sent_again_even_in_the_same_process(
    claude: ClaudeLauncher, anthropic_messages: AnthropicMessages
) -> None:
    """An interrupt after a completed thinking block and before the next block leaves a message
    holding only thinking. Claude drops it from the next request of the live process and of a
    resumed one, although its transcript saved the block."""
    await _seed_crashed_session(claude, anthropic_messages)

    async with claude.start(anthropic_messages, resume_id=CRASHED_SESSION) as first:
        interrupted = await first.send(INTERRUPTED_THINKING_INPUT)
        async with await anthropic_messages.await_next_request() as exchange:
            await exchange.send(
                *anthropic_sse.message_stream(
                    [
                        anthropic_sse.Thinking("INTERRUPTED_THOUGHT", "sig_interrupted"),
                        anthropic_sse.Text("NEVER_SENT"),
                    ],
                    model=MODEL,
                )
                .through("content_block_stop")
                .events
            )
            await _thinking_completed(first)
            assert (await first.interrupt(cancel_queued=False)).response.subtype == "success"
            await exchange.wait_client_closed()
        assert (await interrupted.result()).is_error is True

        live = await first.send(RECOVERY_INPUT)
        async with await anthropic_messages.await_next_request() as exchange:
            assert exchange.request.thinking_blocks == []
            await exchange.send(*anthropic_sse.message_stream([anthropic_sse.Text("LIVE_OK")], model=MODEL).events)
        assert (await live.result()).result == "LIVE_OK"
        await _until(lambda: "LIVE_OK" in _transcript(claude))
        assert "INTERRUPTED_THOUGHT" in _transcript(claude)
        assert await first.crash() < 0

    async with claude.start(anthropic_messages, resume_id=CRASHED_SESSION) as resumed:
        recovery = await resumed.send(RESUMED_AFTER_INTERRUPT_INPUT)
        async with await anthropic_messages.await_next_request() as exchange:
            assert exchange.request.thinking_blocks == []
            await exchange.send(*anthropic_sse.message_stream([anthropic_sse.Text("RESUMED_OK")], model=MODEL).events)
        assert (await recovery.result()).result == "RESUMED_OK"


@pytest.mark.parametrize(
    ("answer", "cut_kind", "cut_nth"),
    [
        pytest.param(anthropic_sse.Text("NEVER_WRITTEN"), "content_block_start", 1, id="text-before-its-first-content"),
        pytest.param(
            anthropic_sse.ToolUse("toolu_never_written", "Bash", {"command": "echo NEVER_WRITTEN"}),
            "input_json_delta",
            0,
            id="tool-call-mid-input",
        ),
    ],
)
async def test_thinking_beside_a_block_interrupted_before_it_was_written_is_not_sent_again(
    claude: ClaudeLauncher,
    anthropic_messages: AnthropicMessages,
    answer: anthropic_sse.Block,
    cut_kind: str,
    cut_nth: int,
) -> None:
    """The block never reaches Claude's context, so its message holds only the thinking: dropped."""
    async with claude.start(anthropic_messages) as run:
        interrupted = await run.send(INTERRUPTED_THINKING_INPUT)
        async with await anthropic_messages.await_next_request() as exchange:
            stream = anthropic_sse.message_stream(
                [anthropic_sse.Thinking("UNWRITTEN_ANSWER_THOUGHT", "sig_unwritten"), answer], model=MODEL
            )
            cut = [index for index, event in enumerate(stream.events) if event.kind == cut_kind][cut_nth]
            await exchange.send(*stream.events[: cut + 1])
            await _thinking_completed(run)
            assert (await run.interrupt(cancel_queued=False)).response.subtype == "success"
            await exchange.wait_client_closed()
        assert (await interrupted.result()).is_error is True

        next_input = await run.send(RECOVERY_INPUT)
        async with await anthropic_messages.await_next_request() as exchange:
            assert exchange.request.thinking_blocks == []
            await exchange.send(*anthropic_sse.message_stream([anthropic_sse.Text("NEXT_OK")], model=MODEL).events)
        assert (await next_input.result()).result == "NEXT_OK"


if __name__ == "__main__":
    pytest_bazel.main()
