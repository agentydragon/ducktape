"""Plain turns: one scripted exchange, and native session resume across processes."""

from __future__ import annotations

import json
from collections.abc import Callable

import pytest
import pytest_bazel
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_delay, wait_fixed

from agentplane.harness_tests.claude import anthropic_sse as sse, frames
from agentplane.harness_tests.claude.harness import MODEL, ClaudeHarness
from agentplane.harness_tests.claude.messages import AnthropicMessages
from agentplane.native.claude import async_run, wire
from agentplane.native.claude.blocks import TextBlock, ThinkingBlock, blocks_of
from agentplane.native.claude.scenarios import SYSTEM_PROMPT, TOOLS

CRASHED_SESSION = "00000000-0000-4000-8000-000000000001"
SEED_INPUT = "Reply with exactly: CRASH_RESUME_SEED_OK"
IN_FLIGHT_INPUT = "Reply with exactly: CRASHED_IN_FLIGHT_FATE"
QUEUED_INPUT = "Reply with exactly: CRASHED_QUEUE_FATE"
RECOVERY_INPUT = "Reply with exactly: CRASH_RESUME_OK"
INTERRUPTED_RESUME_INPUT = "Reply with exactly: INTERRUPTED_RESUME_INPUT"
INTERRUPTED_RESUME_PARTIAL = "INTERRUPTED_RESUME_PARTIAL"
INTERRUPTED_RESUME_RECOVERY = "Reply with exactly: INTERRUPTED_RESUME_RECOVERY_OK"
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


def _transcript(claude: ClaudeHarness) -> str:
    (path,) = (claude.config / "projects").glob(f"*/{CRASHED_SESSION}.jsonl")
    return path.read_text()


async def _thinking_completed(run: async_run.ClaudeRun) -> None:
    events = run.events()
    while True:
        frame = await events.next()
        if isinstance(frame, wire.AssistantFrame) and any(
            isinstance(block, ThinkingBlock) for block in frame.message.content
        ):
            return


async def _seed_crashed_session(claude: ClaudeHarness, anthropic_messages: AnthropicMessages) -> wire.ResultFrame:
    """Complete one turn under CRASHED_SESSION for the crash/resume tests below to build on."""
    async with claude.start(anthropic_messages, session_id=CRASHED_SESSION) as seeded:
        seed_prompt = await seeded.send(SEED_INPUT)
        async with await anthropic_messages.await_next_request() as exchange:
            await exchange.send(*sse.message_stream([sse.Text("CRASH_RESUME_SEED_OK")], model=MODEL).events)
        seed = await seed_prompt.result()
    assert seed.result == "CRASH_RESUME_SEED_OK"
    assert seed.session_id == CRASHED_SESSION
    return seed


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
    await _seed_crashed_session(claude, anthropic_messages)

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


async def test_resume_after_interrupt_then_crash_drops_retained_queued_inputs(
    claude: ClaudeHarness, anthropic_messages: AnthropicMessages
) -> None:
    """A plain interrupt's reported survivors are process-local, not resumed history.

    Claude says that the two queued native inputs survived an active-turn interrupt.  Killing that
    harness before it drains them then starts a fresh `--resume` process: the recovery request has
    the completed seed exchange and recovery input only.  This is distinct from normal plain
    interruption (where survivors later coalesce) and from a raw crash (where no interrupt named
    the still-queued UUIDs).
    """
    await _seed_crashed_session(claude, anthropic_messages)

    async with claude.start(anthropic_messages, resume_id=CRASHED_SESSION, replay_user_messages=True) as interrupted:
        active = await interrupted.send("Keep this turn active until the process is killed.")
        async with await anthropic_messages.await_next_request() as exchange:
            await exchange.send(
                *sse.message_stream([sse.Text("never finished")], model=MODEL).through("content_block_start").events
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
        assert await interrupted.crash() < 0

    async with claude.start(anthropic_messages, resume_id=CRASHED_SESSION) as resumed:
        recovery = await resumed.send(RETAINED_QUEUE_RECOVERY)
        async with await anthropic_messages.await_next_request() as exchange:
            replay = exchange.request
            conversation_user_texts = [
                text for text in replay.texts("user") if not text.startswith("<system-reminder>")
            ]
            assert conversation_user_texts == [SEED_INPUT, RETAINED_QUEUE_RECOVERY]
            assert replay.texts("assistant") == ["CRASH_RESUME_SEED_OK"]
            assert RETAINED_QUEUE_FIRST not in replay.texts("user")
            assert RETAINED_QUEUE_SECOND not in replay.texts("user")
            await exchange.send(*sse.message_stream([sse.Text("RETAINED_QUEUE_CRASH_RECOVERY_OK")], model=MODEL).events)
        assert (await recovery.result()).result == "RETAINED_QUEUE_CRASH_RECOVERY_OK"


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
    await _seed_crashed_session(claude, anthropic_messages)

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


async def test_thinking_ahead_of_a_tool_killed_mid_run_is_saved_but_not_replayed(
    claude: ClaudeHarness, anthropic_messages: AnthropicMessages
) -> None:
    """The transcript holds the thinking block as an entry of its own under the id of its message,
    yet the resume loads neither it nor the tool call: a message left holding only thinking is
    dropped. `runner/claude_history.py` mirrors this."""
    await _seed_crashed_session(claude, anthropic_messages)

    async with claude.start(anthropic_messages, resume_id=CRASHED_SESSION) as first:
        await first.send(KILLED_TOOL_INPUT)
        async with await anthropic_messages.await_next_request() as exchange:
            await exchange.send(
                *sse.message_stream(
                    [
                        sse.Thinking("KILLED_TOOL_THOUGHT", "sig_killed_tool"),
                        sse.ToolUse("toolu_killed", "Bash", {"command": "touch tool_started; sleep 60"}),
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
            await exchange.send(*sse.message_stream([sse.Text("CRASH_RESUME_OK")], model=MODEL).events)
        assert (await recovery.result()).result == "CRASH_RESUME_OK"


async def test_thinking_interrupted_before_any_answer_is_not_sent_again_even_in_the_same_process(
    claude: ClaudeHarness, anthropic_messages: AnthropicMessages
) -> None:
    """An interrupt after a completed thinking block and before the next block leaves a message
    holding only thinking. Claude drops it from the next request of the live process and of a
    resumed one, although its transcript saved the block."""
    await _seed_crashed_session(claude, anthropic_messages)

    async with claude.start(anthropic_messages, resume_id=CRASHED_SESSION) as first:
        interrupted = await first.send(INTERRUPTED_THINKING_INPUT)
        async with await anthropic_messages.await_next_request() as exchange:
            await exchange.send(
                *sse.message_stream(
                    [sse.Thinking("INTERRUPTED_THOUGHT", "sig_interrupted"), sse.Text("NEVER_SENT")], model=MODEL
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
            await exchange.send(*sse.message_stream([sse.Text("LIVE_OK")], model=MODEL).events)
        assert (await live.result()).result == "LIVE_OK"
        await _until(lambda: "LIVE_OK" in _transcript(claude))
        assert "INTERRUPTED_THOUGHT" in _transcript(claude)
        assert await first.crash() < 0

    async with claude.start(anthropic_messages, resume_id=CRASHED_SESSION) as resumed:
        recovery = await resumed.send(RESUMED_AFTER_INTERRUPT_INPUT)
        async with await anthropic_messages.await_next_request() as exchange:
            assert exchange.request.thinking_blocks == []
            await exchange.send(*sse.message_stream([sse.Text("RESUMED_OK")], model=MODEL).events)
        assert (await recovery.result()).result == "RESUMED_OK"


@pytest.mark.parametrize(
    ("answer", "cut_kind", "cut_nth"),
    [
        pytest.param(sse.Text("NEVER_WRITTEN"), "content_block_start", 1, id="text-before-its-first-content"),
        pytest.param(
            sse.ToolUse("toolu_never_written", "Bash", {"command": "echo NEVER_WRITTEN"}),
            "input_json_delta",
            0,
            id="tool-call-mid-input",
        ),
    ],
)
async def test_thinking_beside_a_block_interrupted_before_it_was_written_is_not_sent_again(
    claude: ClaudeHarness, anthropic_messages: AnthropicMessages, answer: sse.Block, cut_kind: str, cut_nth: int
) -> None:
    """The block never reaches Claude's context, so its message holds only the thinking: dropped."""
    async with claude.start(anthropic_messages) as run:
        interrupted = await run.send(INTERRUPTED_THINKING_INPUT)
        async with await anthropic_messages.await_next_request() as exchange:
            stream = sse.message_stream(
                [sse.Thinking("UNWRITTEN_ANSWER_THOUGHT", "sig_unwritten"), answer], model=MODEL
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
            await exchange.send(*sse.message_stream([sse.Text("NEXT_OK")], model=MODEL).events)
        assert (await next_input.result()).result == "NEXT_OK"


if __name__ == "__main__":
    pytest_bazel.main()
